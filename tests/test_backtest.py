"""Tests for backtesting engine."""

from datetime import date

import pandas as pd
import numpy as np
import pytest

from quanti.backtest.commission import AShareCommission
from quanti.backtest.metrics import compute_metrics
from quanti.backtest.engine import BacktestEngine
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.models import Direction, Signal
from quanti.strategy.base import BaseStrategy


class TestAShareCommission:
    def test_buy_commission(self):
        comm = AShareCommission()
        cost = comm.calculate(price=10.0, quantity=1000, direction=Direction.BUY)
        # 佣金 = 10 * 1000 * 0.00025 = 2.5, min 5 => 5.0
        # 过户费 = 10 * 1000 * 0.00001 = 0.1
        assert cost == pytest.approx(5.1)

    def test_sell_commission(self):
        comm = AShareCommission()
        cost = comm.calculate(price=10.0, quantity=1000, direction=Direction.SELL)
        # 佣金 = max(5, 2.5) = 5.0
        # 印花税 = 10 * 1000 * 0.001 = 10.0
        # 过户费 = 0.1
        assert cost == pytest.approx(15.1)


class TestMetrics:
    def test_compute_metrics(self):
        # Simulate a simple equity curve
        np.random.seed(42)
        returns = np.random.randn(252) * 0.01
        equity = 100_000 * (1 + pd.Series(returns)).cumprod()
        metrics = compute_metrics(equity, risk_free_rate=0.03)
        assert "total_return" in metrics
        assert "annual_return" in metrics
        assert "max_drawdown" in metrics
        assert "sharpe_ratio" in metrics
        assert metrics["max_drawdown"] <= 0  # Drawdown is negative


class AlwaysBuyStrategy(BaseStrategy):
    """Buys on first bar, for testing."""

    name = "always_buy"

    def init(self, config):
        self._bought = set()

    def on_bar(self, bar):
        if bar.code not in self._bought:
            self._bought.add(bar.code)
            return [
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=1.0,
                    reason="test buy",
                )
            ]
        return []


class TestBacktestEngine:
    @pytest.fixture
    def setup(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()

        # Insert 30 days of synthetic data
        dates = pd.bdate_range("2024-01-02", periods=30)
        np.random.seed(42)
        prices = 10 + np.cumsum(np.random.randn(30) * 0.1)
        df = pd.DataFrame(
            {
                "code": "000001",
                "date": [d.date() for d in dates],
                "open": prices - 0.1,
                "high": prices + 0.3,
                "low": prices - 0.3,
                "close": prices,
                "volume": np.random.randint(500000, 2000000, 30).astype(float),
                "amount": prices * 1_000_000,
                "turnover": np.random.rand(30) * 3,
            }
        )
        db.save_daily_quotes(df)
        db.save_trade_calendar([d.date() for d in dates])
        provider = DataProvider(db)
        yield db, provider
        db.close()

    def test_backtest_runs(self, setup):
        db, provider = setup
        strategy = AlwaysBuyStrategy()
        strategy.init({})
        engine = BacktestEngine(
            provider=provider,
            initial_cash=100_000.0,
        )
        result = engine.run(
            strategy=strategy,
            codes=["000001"],
            start=date(2024, 1, 1),
            end=date(2024, 2, 28),
        )
        assert result.equity_curve is not None
        assert len(result.equity_curve) > 0
        assert len(result.trades) >= 1  # At least the buy order
        assert result.metrics is not None


def test_backtest_risk_exit_tags_reason(tmp_path):
    """With a RiskManager, a -8% drawdown triggers a stop-loss exit, recorded
    as a `risk_exit` sell carrying the reason — so a backtest shows WHY each
    exit fired and matches the live exit policy. Without a RiskManager (the
    old default) no such exit appears."""
    from quanti.risk.manager import RiskConfig, RiskManager

    db = Database(str(tmp_path / "rx.db"))
    db.initialize()
    # 10 calm bars, a -20% crash on bar 10, then a trailing bar so the
    # stop-loss (detected at the crash close) can fill at the NEXT bar's open
    # — the engine fills at next-open, never the same close.
    dates = pd.bdate_range("2024-01-02", periods=11)
    closes = [10.0, 10.1, 10.05, 10.1, 10.0, 10.1, 10.05, 10.0, 10.1, 8.0, 8.0]
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": closes, "high": [c + 0.1 for c in closes],
        "low": [c - 0.2 for c in closes], "close": closes,
        "volume": 1e6, "amount": [c * 1e6 for c in closes], "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    provider = DataProvider(db)

    # With risk: stop-loss must fire on the crash and be tagged risk_exit.
    s1 = AlwaysBuyStrategy()
    s1.init({})
    res = BacktestEngine(
        provider, 100_000.0,
        risk_manager=RiskManager(RiskConfig(stop_loss_pct=-0.08))
    ).run(s1, ["000001"], date(2024, 1, 1), date(2024, 2, 1))
    exits = [t for t in res.trades if t.strategy == "risk_exit"]
    assert exits, "expected a stop-loss risk_exit on the crash"
    assert any("止损" in t.reason for t in exits)

    # Without risk: no risk_exit at all (old UI/Selector behavior).
    s2 = AlwaysBuyStrategy()
    s2.init({})
    res2 = BacktestEngine(provider, 100_000.0).run(
        s2, ["000001"], date(2024, 1, 1), date(2024, 2, 1))
    assert not [t for t in res2.trades if t.strategy == "risk_exit"]
    db.close()
