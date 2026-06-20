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


def test_max_additional_buy_value_enforces_all_caps():
    rm = RiskManager(RiskConfig(max_position_pct=0.10, max_industry_pct=0.30,
                                max_total_position_pct=0.80))
    # Fresh buy, nothing held: single-stock cap binds → 10% of 100k.
    pf = Portfolio(cash=100_000.0)
    assert rm.max_additional_buy_value(pf, "000001", "银行") == pytest.approx(10_000.0)

    # Same name already at 8% → only 2% single-stock room left.
    pf_stock = Portfolio(cash=92_000.0, positions={
        "000001": Position("000001", 800, 10.0, 10.0, industry="银行")})
    assert pf_stock.total_value == pytest.approx(100_000.0)
    assert rm.max_additional_buy_value(pf_stock, "000001", "银行") == pytest.approx(2_000.0)

    # Industry cap binds: 25% already in 银行, candidate also 银行 → 30%-25% = 5%.
    pf_ind = Portfolio(cash=75_000.0, positions={
        "600000": Position("600000", 2500, 10.0, 10.0, industry="银行")})
    assert rm.max_additional_buy_value(pf_ind, "000001", "银行") == pytest.approx(5_000.0)
    # …but a candidate in a DIFFERENT industry isn't bound by 银行's exposure.
    assert rm.max_additional_buy_value(pf_ind, "000002", "地产") == pytest.approx(10_000.0)

    # Total cap binds: 78% invested → only 2% total room regardless of name.
    pf_total = Portfolio(cash=22_000.0, positions={
        "600000": Position("600000", 3900, 10.0, 10.0, industry="银行"),
        "000002": Position("000002", 3900, 10.0, 10.0, industry="地产")})
    assert pf_total.market_value == pytest.approx(78_000.0)
    assert rm.max_additional_buy_value(pf_total, "300001", "科技") == pytest.approx(2_000.0)

    # Already over a cap → 0 room.
    assert rm.max_additional_buy_value(pf_total, "600000", "银行") == 0.0


def test_check_portfolio_stop():
    rm = RiskManager(RiskConfig(portfolio_stop_loss_pct=-0.15))
    assert rm.check_portfolio_stop(85_000, 100_000) is True    # -15% exactly
    assert rm.check_portfolio_stop(80_000, 100_000) is True    # -20%
    assert rm.check_portfolio_stop(86_000, 100_000) is False   # -14% within
    assert rm.check_portfolio_stop(120_000, 100_000) is False  # new high
    assert rm.check_portfolio_stop(50_000, 0) is False         # no peak yet


def test_daily_cap_auto_resets_on_new_calendar_day(monkeypatch):
    """Live/paper never call reset_daily(); the daily-trade cap must auto-roll
    when the calendar day changes, else it becomes a permanent lifetime lock
    after max_daily_trades trades."""
    import quanti.risk.manager as m
    from datetime import date as _date

    rm = RiskManager(RiskConfig(max_daily_trades=2))
    pf = Portfolio(cash=1_000_000.0)
    buy = Signal(stock_code="000001", direction=Direction.BUY,
                 strength=1.0, reason="x")

    monkeypatch.setattr(m, "date", type("D", (), {
        "today": staticmethod(lambda: _date(2026, 1, 5))}))
    rm.record_trade()
    rm.record_trade()
    ok, reason = rm.check(buy, pf)
    assert not ok and "Daily trade limit" in reason

    # Next calendar day → counter rolls, buys allowed again (no reset_daily call).
    monkeypatch.setattr(m, "date", type("D", (), {
        "today": staticmethod(lambda: _date(2026, 1, 6))}))
    ok2, _ = rm.check(buy, pf)
    assert ok2
