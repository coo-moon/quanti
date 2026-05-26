"""Tests for the slippage models and their integration with the engine.

The volume-impact model is the substantive change — verify that (a) small
orders pay roughly the old flat cost, (b) bigger orders pay more, and (c)
missing ADV degrades gracefully to base cost rather than crashing or
producing NaN fills.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.backtest.engine import BacktestEngine
from quanti.backtest.slippage import FlatSlippage, VolumeImpactSlippage, coerce
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class TestFlatSlippage:
    def test_constant_cost(self):
        s = FlatSlippage(bps=15)
        f = s.adjust(code="x", price=10.0, qty=1000, direction=Direction.BUY, adv20=1e6)
        assert f == pytest.approx(0.0015)

    def test_zero_qty(self):
        s = FlatSlippage(bps=10)
        f = s.adjust(code="x", price=10.0, qty=0, direction=Direction.BUY, adv20=0)
        assert f == 0.001

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            FlatSlippage(bps=-5)


class TestVolumeImpactSlippage:
    def test_small_order_matches_old_flat(self):
        """At ~1% participation, total cost should be ~10 bps — the historical default.
        This guarantees we didn't suddenly make backtests look worse for normal-sized
        orders just by switching the default model."""
        s = VolumeImpactSlippage()
        # 1% of 1M ADV = 10000 元 notional. price 10, qty 1000 = 10000 元.
        f = s.adjust(code="x", price=10.0, qty=1000,
                     direction=Direction.BUY, adv20=1_000_000)
        # base 5 bps + 5 * sqrt(1.0)^0.5 = 5 + 5 = 10 bps
        assert f == pytest.approx(0.0010, rel=0.05)

    def test_large_order_pays_more(self):
        s = VolumeImpactSlippage()
        small = s.adjust(code="x", price=10.0, qty=1000,
                         direction=Direction.BUY, adv20=10_000_000)  # 0.1% ADV
        large = s.adjust(code="x", price=10.0, qty=100_000,
                         direction=Direction.BUY, adv20=10_000_000)  # 10% ADV
        assert large > small
        # Square-root law: 10% / 0.1% = 100×, sqrt = 10×. So impact should grow ~10×.
        # Total grows less (base is constant) but cleanly more than 2×.
        assert large > 2 * small

    def test_missing_adv_falls_back_to_base(self):
        s = VolumeImpactSlippage(base_bps=7)
        f = s.adjust(code="x", price=10.0, qty=1000,
                     direction=Direction.BUY, adv20=0)
        assert f == pytest.approx(0.0007)
        f2 = s.adjust(code="x", price=10.0, qty=1000,
                      direction=Direction.BUY, adv20=None)  # type: ignore[arg-type]
        assert f2 == pytest.approx(0.0007)

    def test_max_bps_caps(self):
        """An insanely large order shouldn't claim 1000% slippage — that's
        more lies than the model is meant to tell."""
        s = VolumeImpactSlippage(max_bps=200)
        f = s.adjust(code="x", price=10.0, qty=10_000_000,
                     direction=Direction.BUY, adv20=1000)  # absurd participation
        assert f <= 0.020  # 200 bps cap

    def test_buy_and_sell_symmetric(self):
        """The model itself is direction-agnostic; the engine applies the sign."""
        s = VolumeImpactSlippage()
        f_buy = s.adjust(code="x", price=10, qty=1000,
                         direction=Direction.BUY, adv20=1e6)
        f_sell = s.adjust(code="x", price=10, qty=1000,
                          direction=Direction.SELL, adv20=1e6)
        assert f_buy == f_sell


class TestCoerce:
    def test_float_becomes_flat(self):
        m = coerce(0.001)
        assert isinstance(m, FlatSlippage)
        # 0.001 fraction → 10 bps → check via adjust
        assert m.adjust(code="x", price=10, qty=1, direction=Direction.BUY, adv20=0) \
            == pytest.approx(0.001)

    def test_model_passes_through(self):
        m = VolumeImpactSlippage()
        assert coerce(m) is m


# ---- Engine integration --------------------------------------------------

class _ForceBuy(BaseStrategy):
    """Emits one BUY on the first bar it sees, then nothing."""
    name = "force_buy"

    def init(self, config: dict) -> None:
        self._fired = False

    def on_bar(self, bar: BarData):
        if self._fired:
            return []
        self._fired = True
        return [Signal(stock_code=bar.code, direction=Direction.BUY,
                       strength=0.5, reason="test")]


@pytest.fixture
def seeded(tmp_path):
    db = Database(str(tmp_path / "slip.db"))
    db.initialize()
    db.upsert_stock("000001", "test", "SZ", date(1991, 4, 3), "test")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=60)
    # Constant price + amount so the only differences come from slippage.
    df = pd.DataFrame({
        "code": "000001",
        "date": [d.date() for d in dates],
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 1_000_000.0,
        "amount": 10_000_000.0,  # 10M 元 ADV
        "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    yield db
    db.close()


class TestEngineIntegration:
    def test_flat_model_fills_at_expected_price(self, seeded):
        provider = DataProvider(seeded)
        engine = BacktestEngine(provider=provider, initial_cash=200_000,
                                slippage=FlatSlippage(bps=10))
        strat = _ForceBuy()
        strat.init({})
        result = engine.run(strategy=strat, codes=["000001"],
                            start=date(2024, 1, 1), end=date.today())
        # Should have at least one buy. With close=10, slippage=10bps → price=10.01
        buys = [t for t in result.trades if t.direction == Direction.BUY]
        assert buys, "expected a buy"
        assert buys[0].price == pytest.approx(10.01, rel=1e-4)

    def test_volume_impact_at_small_participation(self, seeded):
        """200K cash buying 10元 stock = ~20K notional in a 10M ADV name =
        0.2% participation. Expected total ~5 + 5*sqrt(0.2) ≈ 7.2 bps."""
        provider = DataProvider(seeded)
        engine = BacktestEngine(provider=provider, initial_cash=200_000,
                                slippage=VolumeImpactSlippage())
        strat = _ForceBuy()
        strat.init({})
        result = engine.run(strategy=strat, codes=["000001"],
                            start=date(2024, 1, 1), end=date.today())
        buys = [t for t in result.trades if t.direction == Direction.BUY]
        assert buys
        # 200K spent into 10元 stock ≈ 19_800 shares ≈ ~0.2% of 10M ADV.
        # Total slippage ~7 bps → price ~10.007.
        assert 10.005 <= buys[0].price <= 10.015, (
            f"expected price ≈10.007 at ~0.2% participation, got {buys[0].price}")

    def test_legacy_float_still_works(self, seeded):
        """Passing slippage=0.001 (the old style) must still produce a
        valid backtest — the engine wraps it in FlatSlippage."""
        provider = DataProvider(seeded)
        engine = BacktestEngine(provider=provider, initial_cash=200_000,
                                slippage=0.001)
        strat = _ForceBuy()
        strat.init({})
        result = engine.run(strategy=strat, codes=["000001"],
                            start=date(2024, 1, 1), end=date.today())
        buys = [t for t in result.trades if t.direction == Direction.BUY]
        assert buys
        assert buys[0].price == pytest.approx(10.01, rel=1e-4)
