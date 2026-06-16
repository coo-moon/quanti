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


class TestCheckExits:
    """check_exits combines stop-loss + strategy-exit + trailing take-profit."""

    def _pf(self, avg, cur):
        return Portfolio(cash=0.0, positions={
            "X": Position("X", 1000, avg, cur)})

    def test_stoploss_takes_priority(self):
        cfg = RiskConfig(stop_loss_pct=-0.08, take_profit_activate_pct=0.15)
        rm = RiskManager(cfg)
        # -10% loss → stop-loss, even if a strategy also said sell.
        sells = rm.check_exits(self._pf(10.0, 9.0),
                               strategy_sell_codes={"X"})
        assert len(sells) == 1
        assert "止损" in sells[0].reason

    def test_strategy_exit_fires(self):
        rm = RiskManager(RiskConfig())
        # +2% (no stop, not armed for TP) but owning strategy says sell.
        sells = rm.check_exits(self._pf(10.0, 10.2),
                               strategy_sell_codes={"X"})
        assert len(sells) == 1 and "策略" in sells[0].reason

    def test_strategy_exit_respects_flag(self):
        rm = RiskManager(RiskConfig(strategy_exit_enabled=False))
        sells = rm.check_exits(self._pf(10.0, 10.2),
                               strategy_sell_codes={"X"})
        assert sells == []

    def test_trailing_tp_armed_and_retraced(self):
        cfg = RiskConfig(take_profit_activate_pct=0.15, take_profit_trail_pct=0.10)
        rm = RiskManager(cfg)
        # Up +16% (armed); peak 13.0, now 11.6 → -10.8% from peak ≥ 10% → exit.
        sells = rm.check_exits(self._pf(10.0, 11.6), peaks={"X": 13.0})
        assert len(sells) == 1 and "移动止盈" in sells[0].reason

    def test_trailing_tp_not_armed_below_threshold(self):
        cfg = RiskConfig(take_profit_activate_pct=0.15, take_profit_trail_pct=0.10)
        rm = RiskManager(cfg)
        # Only +8% (< activate) — even a big retrace from peak shouldn't exit.
        sells = rm.check_exits(self._pf(10.0, 10.8), peaks={"X": 12.0})
        assert sells == []

    def test_trailing_tp_armed_but_small_retrace_holds(self):
        cfg = RiskConfig(take_profit_activate_pct=0.15, take_profit_trail_pct=0.10)
        rm = RiskManager(cfg)
        # Up +18%, peak 11.9, now 11.8 → only -0.8% from peak → hold.
        sells = rm.check_exits(self._pf(10.0, 11.8), peaks={"X": 11.9})
        assert sells == []

    def test_take_profit_disabled_when_activate_zero(self):
        rm = RiskManager(RiskConfig(take_profit_activate_pct=0.0))
        sells = rm.check_exits(self._pf(10.0, 20.0), peaks={"X": 25.0})
        assert sells == []
