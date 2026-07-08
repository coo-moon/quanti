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

    def test_no_total_position_cap(self, config):
        """Total-position (80%) cap removed: a fresh BUY of a new name passes
        even when the book is already ~92% invested — only the per-stock /
        per-industry caps remain (enforced post-trade in sizing)."""
        rm = RiskManager(config)
        portfolio = Portfolio(
            cash=20_000.0,
            positions={
                "000001": Position("000001", 5000, 10.0, 10.0),
                "600519": Position("600519", 100, 1800.0, 1800.0),
            },
        )
        # 230000/250000 = 92% invested — would have tripped the old 80% cap.
        signal = Signal("000002", Direction.BUY, 0.5, "test")
        allowed, reason = rm.check(signal, portfolio)
        assert allowed is True, reason

    def test_allows_sell_always(self, portfolio, config):
        """Sell signals should always pass risk check."""
        config.max_position_pct = 0.01  # Very restrictive
        rm = RiskManager(config)
        signal = Signal("000001", Direction.SELL, 0.8, "test")
        allowed, _ = rm.check(signal, portfolio)
        assert allowed is True



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

    # --- P1-1: ATR-adaptive stop ---
    def test_atr_stop_tighter_for_calm_name(self):
        # ratio 0.04, k=1 → stop -4%; a -6% loss exits though fixed -8% wouldn't.
        rm = RiskManager(RiskConfig(stop_loss_pct=-0.08, atr_stop_k=1.0))
        sells = rm.check_exits(self._pf(10.0, 9.4), atr_ratios={"X": 0.04})
        assert len(sells) == 1 and "止损" in sells[0].reason
        assert "ATR" in sells[0].reason  # tagged as ATR-driven

    def test_atr_disabled_falls_back_to_floor(self):
        # k=0 → only the stop_loss_pct floor (-8%) governs; -6% holds.
        rm = RiskManager(RiskConfig(stop_loss_pct=-0.08, atr_stop_k=0.0))
        assert rm.check_exits(self._pf(10.0, 9.4), atr_ratios={"X": 0.04}) == []

    def test_atr_stop_wider_for_volatile_name_within_floor(self):
        # Wide floor (-20%), ratio 0.10, k=1 → stop -10%: a -9% loss holds…
        rm = RiskManager(RiskConfig(stop_loss_pct=-0.20, atr_stop_k=1.0))
        assert rm.check_exits(self._pf(10.0, 9.1), atr_ratios={"X": 0.10}) == []
        # …but a -11% loss exits.
        assert len(rm.check_exits(self._pf(10.0, 8.9),
                                  atr_ratios={"X": 0.10})) == 1

    def test_atr_stop_capped_at_floor(self):
        # ratio 0.5, k=1 → -50% but floored at stop_loss_pct -15%: a stop is
        # never wider than the floor. -14% holds, -16% exits.
        rm = RiskManager(RiskConfig(stop_loss_pct=-0.15, atr_stop_k=1.0))
        assert rm.check_exits(self._pf(10.0, 8.6), atr_ratios={"X": 0.5}) == []
        exits = rm.check_exits(self._pf(10.0, 8.4), atr_ratios={"X": 0.5})
        assert len(exits) == 1 and "地板" in exits[0].reason

    def test_atr_armed_but_missing_ratio_uses_floor(self):
        # k>0 but no ratio for the code → the stop_loss_pct floor still applies.
        rm = RiskManager(RiskConfig(stop_loss_pct=-0.08, atr_stop_k=2.0))
        assert len(rm.check_exits(self._pf(10.0, 9.0), atr_ratios={})) == 1
        assert rm.check_exits(self._pf(10.0, 9.4), atr_ratios={}) == []

    # --- P0-4: exit priority is 固化 (locked) ---
    def test_exit_priority_locked(self):
        """止损 > 策略离场 > 移动止盈. Reordering check_exits breaks this."""
        cfg = RiskConfig(stop_loss_pct=-0.08, take_profit_activate_pct=0.15,
                         take_profit_trail_pct=0.10, strategy_exit_enabled=True)
        rm = RiskManager(cfg)
        # Stop-loss beats strategy-exit: -10% loss + strategy says sell.
        s1 = rm.check_exits(self._pf(10.0, 9.0), strategy_sell_codes={"X"})
        assert len(s1) == 1 and "止损" in s1[0].reason
        # Strategy-exit beats trailing-TP: +16% & retraced ≥10% from peak (TP
        # would fire) AND strategy says sell → strategy-exit wins.
        s2 = rm.check_exits(self._pf(10.0, 11.6), peaks={"X": 13.0},
                            strategy_sell_codes={"X"})
        assert len(s2) == 1 and "策略" in s2[0].reason


def test_max_additional_buy_value_enforces_all_caps():
    rm = RiskManager(RiskConfig(max_position_pct=0.10, max_industry_pct=0.30))
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

    # Total-position cap removed: at 78% invested a candidate in a FRESH
    # industry is bounded only by the single-stock 10% cap (10k), not by any
    # 80% total ceiling.
    pf_total = Portfolio(cash=22_000.0, positions={
        "600000": Position("600000", 3900, 10.0, 10.0, industry="银行"),
        "000002": Position("000002", 3900, 10.0, 10.0, industry="地产")})
    assert pf_total.market_value == pytest.approx(78_000.0)
    assert rm.max_additional_buy_value(pf_total, "300001", "科技") == pytest.approx(10_000.0)

    # A name already over the single-stock cap still has 0 room.
    assert rm.max_additional_buy_value(pf_total, "600000", "银行") == 0.0


def test_risk_config_from_dict():
    """P0-3: build a RiskConfig from a partial runtime-override dict; absent or
    None fields keep dataclass defaults, unknown keys are ignored."""
    from quanti.risk.manager import risk_config_from_dict
    assert risk_config_from_dict({}) == RiskConfig()  # empty → all defaults
    c = risk_config_from_dict({"stop_loss_pct": -0.05, "atr_stop_k": 2.0,
                               "bogus": 1, "take_profit_trail_pct": None})
    assert c.stop_loss_pct == -0.05 and c.atr_stop_k == 2.0
    assert c.take_profit_trail_pct == RiskConfig().take_profit_trail_pct


def test_extreme_gap_up_block_pct_persists_and_loads(tmp_path):
    """The gap-up guard threshold round-trips through the risk_config table and
    rebuilds via risk_config_from_dict (so live UI edits reach the brokers)."""
    from quanti.data.database import Database
    from quanti.risk.manager import risk_config_from_dict
    db = Database(str(tmp_path / "rc.db"))
    db.initialize()
    # Default (no row): dataclass default 0.10.
    assert RiskConfig().extreme_gap_up_block_pct == 0.10
    cfg = _full_risk_dict(extreme_gap_up_block_pct=0.0)  # disable
    db.upsert_risk_config(cfg)
    loaded = db.get_risk_config()
    assert loaded["extreme_gap_up_block_pct"] == 0.0
    assert risk_config_from_dict(loaded).extreme_gap_up_block_pct == 0.0
    db.close()


def _full_risk_dict(**over):
    """A complete risk_config dict (all NOT-NULL columns) with overrides."""
    base = dict(
        stop_loss_pct=-0.15, portfolio_stop_loss_pct=-0.30,
        take_profit_activate_pct=0.15, take_profit_trail_pct=0.10,
        strategy_exit_enabled=True, atr_stop_k=2.0, atr_stop_n=14,
        max_position_pct=0.20, max_industry_pct=0.30,
        extreme_gap_up_block_pct=0.10)
    base.update(over)
    return base


def test_extreme_gap_up_blocked_helper():
    """Pure gate: BUY fill price >= block_pct above prior close is blocked;
    below it, missing data, or block_pct<=0 are not."""
    from quanti.utils.market import extreme_gap_up_blocked
    assert extreme_gap_up_blocked(11.0, 10.0, 0.10) is True    # +10% exactly
    assert extreme_gap_up_blocked(11.5, 10.0, 0.10) is True    # +15%
    assert extreme_gap_up_blocked(10.9, 10.0, 0.10) is False   # +9% < 10%
    assert extreme_gap_up_blocked(11.5, 10.0, 0.0) is False    # guard disabled
    assert extreme_gap_up_blocked(11.5, None, 0.10) is False   # no prior close
    assert extreme_gap_up_blocked(11.5, 0.0, 0.10) is False    # bad prior close


def test_check_portfolio_stop():
    rm = RiskManager(RiskConfig(portfolio_stop_loss_pct=-0.15))
    assert rm.check_portfolio_stop(85_000, 100_000) is True    # -15% exactly
    assert rm.check_portfolio_stop(80_000, 100_000) is True    # -20%
    assert rm.check_portfolio_stop(86_000, 100_000) is False   # -14% within
    assert rm.check_portfolio_stop(120_000, 100_000) is False  # new high
    assert rm.check_portfolio_stop(50_000, 0) is False         # no peak yet


def test_check_exits_stop_loss_reason_uses_prefix():
    """Stop-loss exits use the prefix; take-profit and strategy-exit do NOT."""
    from quanti.models import Portfolio, Position
    from quanti.risk.manager import (
        RiskManager, RiskConfig, STOP_LOSS_REASON_PREFIX,
        STRATEGY_EXIT_REASON_PREFIX,
    )
    cfg = RiskConfig(stop_loss_pct=-0.08, take_profit_activate_pct=0.15,
                     take_profit_trail_pct=0.10)
    rm = RiskManager(cfg)

    # 1. Stop-loss: position down -10% → emitted reason STARTS WITH prefix.
    pf_sl = Portfolio(cash=0.0, positions={
        "000001": Position(stock_code="000001", quantity=1000,
                           avg_cost=10.0, current_price=9.0)})
    sells_sl = rm.check_exits(pf_sl)
    assert sells_sl and sells_sl[0].reason.startswith(STOP_LOSS_REASON_PREFIX)

    # 2. Trailing take-profit: up +18%, peak 13.0, now 11.6 → -10.8% retrace.
    #    Emitted reason must be non-empty and must NOT start with stop-loss prefix.
    pf_tp = Portfolio(cash=0.0, positions={
        "000002": Position(stock_code="000002", quantity=1000,
                           avg_cost=10.0, current_price=11.6)})
    sells_tp = rm.check_exits(pf_tp, peaks={"000002": 13.0})
    assert sells_tp, "expected trailing take-profit exit"
    assert sells_tp[0].reason  # non-empty
    assert not sells_tp[0].reason.startswith(STOP_LOSS_REASON_PREFIX)

    # 3. Strategy-exit: no loss, no TP threshold, but strategy says SELL.
    #    Emitted reason must be non-empty and must NOT start with stop-loss prefix.
    pf_se = Portfolio(cash=0.0, positions={
        "000003": Position(stock_code="000003", quantity=1000,
                           avg_cost=10.0, current_price=10.2)})
    sells_se = rm.check_exits(pf_se, strategy_sell_codes={"000003"})
    assert sells_se, "expected strategy-exit"
    assert sells_se[0].reason  # non-empty
    assert not sells_se[0].reason.startswith(STOP_LOSS_REASON_PREFIX)
    # strategy-exit uses its OWN prefix so audit/UI can tell it apart from
    # stop-loss / take-profit (all three share strategy_name 'risk_exit').
    assert sells_se[0].reason.startswith(STRATEGY_EXIT_REASON_PREFIX)

    # 4. dict input names the owning strategy → reason carries it, so the audit
    #    shows WHICH strategy said sell (not just that *a* risk_exit fired).
    sells_named = rm.check_exits(
        pf_se, strategy_sell_codes={"000003": "macd_cross"})
    assert sells_named and "macd_cross" in sells_named[0].reason
    assert sells_named[0].reason.startswith(STRATEGY_EXIT_REASON_PREFIX)


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


def test_daily_cap_counts_opens_not_exits():
    """The daily-trade cap limits NEW positions (opens). A cluster of SELLs
    (stop-loss/flatten) must NOT consume the budget and block rebalancing
    BUYs (audit F2)."""
    rm = RiskManager(RiskConfig(max_daily_trades=2))
    pf = Portfolio(cash=1_000_000.0)
    buy = Signal("000001", Direction.BUY, 0.5, "b")
    # Many exits today — none count against the open budget.
    for _ in range(5):
        rm.record_trade(Direction.SELL)
    assert rm.check(buy, pf)[0] is True
    # Two opens consume the cap; the third open is blocked.
    rm.record_trade(Direction.BUY)
    rm.record_trade(Direction.BUY)
    ok, reason = rm.check(buy, pf)
    assert ok is False and "limit" in reason.lower()


def test_seed_daily_trades_blocks_restart_bypass():
    """G2: seeding today's open-count (from the venue at session start) keeps the
    daily cap intact across a mid-day process restart — which would otherwise
    reset the in-memory count to 0 and let max_daily_trades be bypassed."""
    rm = RiskManager(RiskConfig(max_daily_trades=2))
    pf = Portfolio(cash=1_000_000.0)
    buy = Signal("000001", Direction.BUY, 0.5, "b")
    rm.seed_daily_trades(2)                 # 2 opens already done today per venue
    ok, reason = rm.check(buy, pf)
    assert ok is False and "limit" in reason.lower()   # cap already reached
    rm.seed_daily_trades(1)                 # never LOWERS an existing count
    assert rm._daily_trade_count == 2
