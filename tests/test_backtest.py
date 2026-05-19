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
from quanti.models import BarData, Direction, Signal
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
        dates = pd.date_range("2024-01-01", periods=252, freq="B")
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
