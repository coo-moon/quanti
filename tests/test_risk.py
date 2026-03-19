"""Tests for risk management module."""

import pytest

from quanti.models import Direction, Portfolio, Position, Signal
from quanti.risk.manager import RiskManager, RiskConfig


@pytest.fixture
def portfolio():
    return Portfolio(
        cash=100_000.0,
        positions={
            "000001": Position(
                stock_code="000001", quantity=5000, avg_cost=10.0, current_price=10.0
            ),
        },
    )


@pytest.fixture
def config():
    return RiskConfig()


class TestRiskManager:
    def test_signal_passes_risk_check(self, portfolio, config):
        rm = RiskManager(config)
        signal = Signal(
            stock_code="600519",
            direction=Direction.BUY,
            strength=0.8,
            reason="test",
        )
        allowed, reason = rm.check(signal, portfolio)
        assert allowed is True

    def test_rejects_over_position_limit(self, portfolio, config):
        """Single stock position exceeds max_position_pct."""
        config.max_position_pct = 0.1  # 10%
        rm = RiskManager(config)
        # 000001 already at 50000/150000 = 33%, reject more buys
        signal = Signal(
            stock_code="000001",
            direction=Direction.BUY,
            strength=0.8,
            reason="test",
        )
        allowed, reason = rm.check(signal, portfolio)
        assert allowed is False
        assert "position" in reason.lower()

    def test_rejects_at_max_total_position(self, config):
        """Total position exceeds max_total_position_pct."""
        config.max_total_position_pct = 0.5
        rm = RiskManager(config)
        portfolio = Portfolio(
            cash=20_000.0,
            positions={
                "000001": Position("000001", 5000, 10.0, 10.0),
                "600519": Position("600519", 100, 1800.0, 1800.0),
            },
        )
        # Total position = 50000 + 180000 = 230000, total = 250000
        # Position ratio = 92%, exceeds 50%
        signal = Signal("000002", Direction.BUY, 0.5, "test")
        allowed, reason = rm.check(signal, portfolio)
        assert allowed is False

    def test_allows_sell_always(self, portfolio, config):
        """Sell signals should always pass risk check."""
        config.max_position_pct = 0.01  # Very restrictive
        rm = RiskManager(config)
        signal = Signal("000001", Direction.SELL, 0.8, "test")
        allowed, _ = rm.check(signal, portfolio)
        assert allowed is True

    def test_stop_loss_check(self, config):
        config.stop_loss_pct = -0.08
        rm = RiskManager(config)
        portfolio = Portfolio(
            cash=100_000.0,
            positions={
                "000001": Position("000001", 1000, 10.0, 9.0),  # -10% loss
            },
        )
        stop_signals = rm.check_stop_loss(portfolio)
        assert len(stop_signals) == 1
        assert stop_signals[0].direction == Direction.SELL
