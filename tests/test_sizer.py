"""Tests for position sizing strategies.

Vol-targeting is the substantive change here. Verify that:
  - At equal strength, a low-vol stock gets a bigger weight than a high-vol one.
  - The hard per-stock cap still binds (sizer can't override RiskManager).
  - Missing/insufficient history falls back to a conservative weight.
  - The FixedSizer reproduces the legacy "max_pct × strength" behavior.
  - Plugged into PaperBroker, vol-targeting actually shrinks high-vol fills.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.models import BarData, Direction, Signal
from quanti.risk.sizer import FixedSizer, VolTargetSizer


def _make_bars(code: str, n: int, sigma_per_day: float, start_price: float = 10.0,
               seed: int = 0) -> list[BarData]:
    np.random.seed(seed)
    rets = np.random.randn(n) * sigma_per_day
    prices = start_price * np.exp(np.cumsum(rets))
    bars = []
    today = date.today()
    for i, p in enumerate(prices):
        bars.append(BarData(
            code=code,
            date=date.fromordinal(today.toordinal() - (n - i)),
            open=float(p), high=float(p) * 1.01,
            low=float(p) * 0.99, close=float(p),
            volume=1_000_000, amount=float(p) * 1_000_000, turnover=1.0,
        ))
    return bars


class TestFixedSizer:
    def test_scales_with_strength(self):
        s = FixedSizer(max_pct=0.10)
        bars: list[BarData] = []
        w_full = s.target_weight(code="x", signal_strength=1.0,
                                 recent_bars=bars, portfolio_total_value=1e6)
        w_half = s.target_weight(code="x", signal_strength=0.5,
                                 recent_bars=bars, portfolio_total_value=1e6)
        assert w_full == pytest.approx(0.10)
        assert w_half == pytest.approx(0.05)

    def test_zero_strength_zero_weight(self):
        s = FixedSizer()
        assert s.target_weight(code="x", signal_strength=0,
                               recent_bars=[], portfolio_total_value=1e6) == 0.0

    def test_clamps_strength_above_1(self):
        s = FixedSizer(max_pct=0.10)
        w = s.target_weight(code="x", signal_strength=5.0,
                            recent_bars=[], portfolio_total_value=1e6)
        assert w == pytest.approx(0.10)


class TestVolTargetSizer:
    def test_low_vol_gets_bigger_weight_than_high_vol(self):
        """The core promise of vol-targeting: σ_high > σ_low ⇒ weight_high < weight_low."""
        sizer = VolTargetSizer(target_portfolio_vol=0.18, n_target_positions=10,
                               max_pct=0.10)
        # Low vol: ~12% annual (≈ daily 0.75%)
        bars_low = _make_bars("LOW", 80, sigma_per_day=0.0075, seed=1)
        # High vol: ~50% annual (≈ daily 3.15%)
        bars_high = _make_bars("HIGH", 80, sigma_per_day=0.0315, seed=2)

        w_low = sizer.target_weight(code="LOW", signal_strength=1.0,
                                    recent_bars=bars_low, portfolio_total_value=1e6)
        w_high = sizer.target_weight(code="HIGH", signal_strength=1.0,
                                     recent_bars=bars_high, portfolio_total_value=1e6)
        assert w_low > w_high

    def test_hard_max_pct_caps_low_vol(self):
        """Even with σ→0, weight should never exceed max_pct."""
        sizer = VolTargetSizer(max_pct=0.10)
        bars = _make_bars("FLAT", 80, sigma_per_day=0.0001, seed=3)
        w = sizer.target_weight(code="FLAT", signal_strength=1.0,
                                recent_bars=bars, portfolio_total_value=1e6)
        assert w <= 0.10 + 1e-9

    def test_insufficient_history_uses_fallback(self):
        sizer = VolTargetSizer(max_pct=0.10, min_bars=20)
        # Only 5 bars — way under min_bars threshold.
        bars = _make_bars("NEW", 5, sigma_per_day=0.02, seed=4)
        w = sizer.target_weight(code="NEW", signal_strength=1.0,
                                recent_bars=bars, portfolio_total_value=1e6)
        # Fallback is max_pct * 0.5 * strength = 0.05
        assert w == pytest.approx(0.05)

    def test_strength_scales_weight(self):
        sizer = VolTargetSizer()
        bars = _make_bars("X", 80, sigma_per_day=0.015, seed=5)
        w_full = sizer.target_weight(code="X", signal_strength=1.0,
                                     recent_bars=bars, portfolio_total_value=1e6)
        w_half = sizer.target_weight(code="X", signal_strength=0.5,
                                     recent_bars=bars, portfolio_total_value=1e6)
        # Half strength should give approximately half weight (modulo floors).
        assert w_half <= w_full
        # Either reaches the floor, or is roughly half.
        assert w_half >= sizer._floor * 0.5
        # Tolerance for the floor case
        if w_full < 0.10 and w_half > sizer._floor:
            assert w_half == pytest.approx(w_full / 2, rel=0.05)

    def test_zero_strength_zero_weight(self):
        sizer = VolTargetSizer()
        bars = _make_bars("X", 80, sigma_per_day=0.015, seed=6)
        assert sizer.target_weight(code="X", signal_strength=0.0,
                                   recent_bars=bars,
                                   portfolio_total_value=1e6) == 0.0


# ----- broker integration --------------------------------------------------

@pytest.fixture
def broker_with_data(tmp_path):
    """Set up a broker with two stocks of very different vol."""
    db = Database(str(tmp_path / "sizer.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=80)

    # Low-vol stock (~10% annual)
    np.random.seed(11)
    low_rets = np.random.randn(len(dates)) * 0.006
    low_prices = 10 * np.exp(np.cumsum(low_rets))
    # High-vol stock (~50% annual)
    np.random.seed(22)
    high_rets = np.random.randn(len(dates)) * 0.031
    high_prices = 10 * np.exp(np.cumsum(high_rets))

    db.upsert_stock("000001", "low-vol", "SZ", date(1991, 4, 3), "银行")
    db.upsert_stock("000002", "high-vol", "SZ", date(1991, 4, 3), "科技")

    for code, prices in [("000001", low_prices), ("000002", high_prices)]:
        df = pd.DataFrame({
            "code": code,
            "date": [d.date() for d in dates],
            "open": prices, "high": prices * 1.01, "low": prices * 0.99,
            "close": prices,
            "volume": np.full(len(dates), 5_000_000.0),
            "amount": prices * 5_000_000,
            "turnover": np.full(len(dates), 1.0),
        })
        db.save_daily_quotes(df)

    provider = DataProvider(db)
    yield db, provider
    db.close()


class TestPaperBrokerWithSizer:
    def test_vol_target_buys_less_high_vol(self, broker_with_data):
        """At identical strength, the high-vol stock should attract less capital."""
        db, provider = broker_with_data
        sizer = VolTargetSizer(target_portfolio_vol=0.18, max_pct=0.10,
                               n_target_positions=10, min_bars=20)
        broker = PaperBroker(db, provider, initial_cash=1_000_000, sizer=sizer)

        # Same strength on both — only the sizer should differentiate them.
        broker.execute_signal(
            Signal(stock_code="000001", direction=Direction.BUY,
                   strength=1.0, reason="low vol"),
            "test")
        broker.execute_signal(
            Signal(stock_code="000002", direction=Direction.BUY,
                   strength=1.0, reason="high vol"),
            "test")
        positions = {p["code"]: p for p in db.list_positions()}
        # Both should fill (vol-target won't reject either), but low-vol
        # should hold more notional value than high-vol.
        assert "000001" in positions, "low-vol buy didn't fill"
        assert "000002" in positions, "high-vol buy didn't fill"
        low_notional = positions["000001"]["quantity"] * positions["000001"]["avg_cost"]
        high_notional = positions["000002"]["quantity"] * positions["000002"]["avg_cost"]
        assert low_notional > high_notional, (
            f"expected low-vol position ({low_notional:.0f}) > "
            f"high-vol position ({high_notional:.0f})")

    def test_no_sizer_preserves_legacy_behavior(self, broker_with_data):
        """With no sizer, broker should behave exactly as before. The
        existing test_paper_broker.py tests rely on this — this test
        documents that decision."""
        db, provider = broker_with_data
        broker = PaperBroker(db, provider, initial_cash=200_000)  # no sizer
        sig = Signal(stock_code="000001", direction=Direction.BUY,
                     strength=0.5, reason="legacy")
        assert broker.execute_signal(sig, "test") is True
        positions = db.list_positions()
        assert len(positions) == 1
        assert positions[0]["quantity"] >= 100
