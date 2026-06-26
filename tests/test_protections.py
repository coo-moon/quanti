# tests/test_protections.py
from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from quanti.risk.protections import (
    ProtectionConfig, ProtectionContext, ProtectionManager,
)


def _consecutive_td(a: date, b: date) -> int:
    """Trading-day distance for tests using consecutive calendar days as
    trading days: counts days in (a, b]."""
    return (b - a).days if b > a else 0


def _ctx(today, sl_dates=None, equity=None):
    return ProtectionContext(
        today=today,
        stop_loss_exit_dates=sl_dates or [],
        equity_series=equity or [],
        trading_days_between=_consecutive_td,
    )


D0 = date(2026, 6, 1)


def _d(n: int) -> date:
    return D0 + timedelta(days=n)


# ---- StoplossGuard ----------------------------------------------------

def test_stoploss_guard_below_limit_allows():
    # 2 stops in window, limit is 3 → allowed.
    mgr = ProtectionManager(ProtectionConfig())
    ctx = _ctx(_d(4), sl_dates=[_d(1), _d(2)])
    assert mgr.check_entry(ctx) == (True, "")


def test_stoploss_guard_locks_after_n_stops_for_k_days():
    mgr = ProtectionManager(ProtectionConfig())  # W=5 N=3 K=5
    # 3 stops within 5 trading days → trigger on _d(3); locked through _d(8).
    locked_ctx = _ctx(_d(3), sl_dates=[_d(1), _d(2), _d(3)])
    allowed, reason = mgr.check_entry(locked_ctx)
    assert allowed is False
    assert "StoplossGuard" in reason
    # Still locked on the K-th trading day after the trigger (_d(3)+5 = _d(8)).
    assert mgr.check_entry(_ctx(_d(8), sl_dates=[_d(1), _d(2), _d(3)]))[0] is False
    # Unlocked on K+1 (_d(9)).
    assert mgr.check_entry(_ctx(_d(9), sl_dates=[_d(1), _d(2), _d(3)]))[0] is True


def test_stoploss_guard_extends_on_new_trigger():
    mgr = ProtectionManager(ProtectionConfig())
    # Stops at d1,d2,d3 (trigger d3) then another cluster d6,d7 keeping >=3 in
    # the 5-day window ending d7 (d3..d7) → trigger d7, lock extends to d12.
    dates = [_d(1), _d(2), _d(3), _d(6), _d(7)]
    assert mgr.check_entry(_ctx(_d(11), sl_dates=dates))[0] is False
    assert mgr.check_entry(_ctx(_d(13), sl_dates=dates))[0] is True


def test_stoploss_guard_disabled():
    cfg = ProtectionConfig(stoploss_guard_enabled=False)
    mgr = ProtectionManager(cfg)
    assert mgr.check_entry(_ctx(_d(3), sl_dates=[_d(1), _d(2), _d(3)]))[0] is True


# ---- MaxDrawdown ------------------------------------------------------

def _equity(values_by_offset):
    return [(_d(n), v) for n, v in values_by_offset]


def test_max_drawdown_above_threshold_allows():
    mgr = ProtectionManager(ProtectionConfig())  # thr=-0.08 minpts=5
    eq = _equity([(0, 100), (1, 99), (2, 100), (3, 101), (4, 100)])  # ~-1%
    assert mgr.check_entry(_ctx(_d(4), equity=eq))[0] is True


def test_max_drawdown_locks_on_window_peak_to_trough():
    mgr = ProtectionManager(ProtectionConfig())
    # Peak 100 → trough 88 = -12% within window → lock; threshold -8%.
    eq = _equity([(0, 100), (1, 96), (2, 92), (3, 90), (4, 88)])
    allowed, reason = mgr.check_entry(_ctx(_d(4), equity=eq))
    assert allowed is False
    assert "MaxDrawdown" in reason


def test_max_drawdown_uses_true_peak_to_trough_not_current_point():
    # Deep dip then partial bounce: current vs peak is only -7%, but the window
    # peak-to-trough is -12% → must still lock (the bug the design fixes).
    mgr = ProtectionManager(ProtectionConfig())
    eq = _equity([(0, 100), (1, 95), (2, 90), (3, 88), (4, 93)])  # trough 88 = -12%
    assert mgr.check_entry(_ctx(_d(4), equity=eq))[0] is False


def test_max_drawdown_fail_open_on_thin_window():
    mgr = ProtectionManager(ProtectionConfig())  # minpts=5
    eq = _equity([(0, 100), (1, 80)])  # -20% but only 2 points
    assert mgr.check_entry(_ctx(_d(1), equity=eq))[0] is True


def test_max_drawdown_unlocks_after_k_days():
    cfg = ProtectionConfig(md_lock_days=2, md_lookback_days=5, md_min_points=3)
    mgr = ProtectionManager(cfg)
    eq = _equity([(0, 100), (1, 96), (2, 90)])  # trigger at _d(2)
    assert mgr.check_entry(_ctx(_d(2), equity=eq))[0] is False  # day 0 after
    assert mgr.check_entry(_ctx(_d(4), equity=eq))[0] is False  # K-th day
    assert mgr.check_entry(_ctx(_d(5), equity=eq))[0] is True   # K+1


def test_max_drawdown_disabled():
    cfg = ProtectionConfig(max_drawdown_enabled=False)
    mgr = ProtectionManager(cfg)
    eq = _equity([(0, 100), (1, 96), (2, 92), (3, 90), (4, 88)])  # -12%
    assert mgr.check_entry(_ctx(_d(4), equity=eq))[0] is True


# ---- Aggregation ------------------------------------------------------

def test_check_entry_first_lock_wins_and_disabled_passes():
    mgr = ProtectionManager(ProtectionConfig(enabled=False))
    eq = _equity([(0, 100), (1, 96), (2, 92), (3, 90), (4, 88)])
    assert mgr.check_entry(_ctx(_d(4), sl_dates=[_d(2), _d(3), _d(4)],
                                equity=eq)) == (True, "")


def test_check_entry_code_param_ignored():
    mgr = ProtectionManager(ProtectionConfig())
    ctx = _ctx(_d(3), sl_dates=[_d(1), _d(2), _d(3)])  # StoplossGuard locks
    assert mgr.check_entry(ctx, code="000001") == mgr.check_entry(ctx)
    assert mgr.check_entry(ctx, code="000001")[0] is False


# ---- CorrelationGuard -------------------------------------------------

def _cg_cfg(**kw):
    base = dict(correlation_guard_enabled=True, cg_lookback_days=20,
                cg_max_avg_corr=0.75, cg_min_holdings=5,
                stoploss_guard_enabled=False, max_drawdown_enabled=False)
    base.update(kw)
    return ProtectionConfig(**base)


def _cg_ctx(holdings_returns):
    return ProtectionContext(
        today=_d(30), stop_loss_exit_dates=[], equity_series=[],
        trading_days_between=_consecutive_td, holdings_returns=holdings_returns)


def test_correlation_guard_locks_when_book_is_one_bet():
    rng = np.random.default_rng(1)
    factor = rng.normal(0, 0.02, 25)
    # 5 holdings = the same factor + tiny idiosyncratic noise → avg corr ~1.
    hr = {f"c{i}": (factor + rng.normal(0, 0.001, 25)).tolist() for i in range(5)}
    allowed, reason = ProtectionManager(_cg_cfg()).check_entry(_cg_ctx(hr))
    assert allowed is False
    assert "CorrelationGuard" in reason


def test_correlation_guard_allows_diversified_book():
    rng = np.random.default_rng(2)
    hr = {f"c{i}": rng.normal(0, 0.02, 40).tolist() for i in range(6)}  # independent
    assert ProtectionManager(_cg_cfg()).check_entry(_cg_ctx(hr))[0] is True


def test_correlation_guard_fail_open_below_min_holdings():
    rng = np.random.default_rng(3)
    factor = rng.normal(0, 0.02, 25).tolist()
    hr = {f"c{i}": factor for i in range(3)}  # corr=1 but only 3 < min 5
    assert ProtectionManager(_cg_cfg()).check_entry(_cg_ctx(hr))[0] is True


def test_correlation_guard_off_by_default():
    rng = np.random.default_rng(4)
    factor = rng.normal(0, 0.02, 25).tolist()
    hr = {f"c{i}": factor for i in range(6)}  # identical → corr 1
    # Default config has correlation_guard_enabled=False → never locks on it.
    assert ProtectionManager(ProtectionConfig()).check_entry(_cg_ctx(hr))[0] is True


def test_correlation_guard_drops_zero_variance_then_fail_open():
    rng = np.random.default_rng(5)
    factor = rng.normal(0, 0.02, 25).tolist()
    hr = {f"c{i}": factor for i in range(3)}          # 3 correlated...
    hr.update({f"flat{i}": [0.0] * 25 for i in range(3)})  # ...+3 flat (dropped)
    # After dropping zero-variance holdings only 3 remain (< min 5) → fail-open.
    assert ProtectionManager(_cg_cfg()).check_entry(_cg_ctx(hr))[0] is True


# ---- Live context builder ---------------------------------------------

def test_build_db_context_from_database(tmp_path):
    from datetime import date, datetime
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.risk.protections import ProtectionConfig
    from quanti.risk.protection_context import build_db_context

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.ensure_portfolio(100_000)

    def iso(d):
        return datetime(d.year, d.month, d.day, 15, 0).isoformat()

    db.insert_order({
        "order_id": "o1", "code": "000001", "direction": "sell",
        "quantity": 100, "price_type": "market", "limit_price": 0.0,
        "status": "filled", "strategy_name": "risk_exit",
        "filled_price": 9.0, "filled_quantity": 100,
        "reason": "止损 -10% ≤ -8%", "created_at": iso(date(2026, 6, 18)),
        "filled_at": iso(date(2026, 6, 18)), "entry_strategy": "",
    })
    db.save_portfolio_snapshot(date(2026, 6, 18), 50_000, 50_000, 100_000)
    db.save_portfolio_snapshot(date(2026, 6, 19), 50_000, 48_000, 98_000)

    ctx = build_db_context(db, DataProvider(db), ProtectionConfig(),
                           today=date(2026, 6, 20))
    assert date(2026, 6, 18) in ctx.stop_loss_exit_dates
    assert any(v == 100_000 for _d, v in ctx.equity_series)
    assert ctx.today == date(2026, 6, 20)
    assert callable(ctx.trading_days_between)


# ---- evaluate_entry shared entry gate ---------------------------------

def test_evaluate_entry_gates_buy_not_sell(tmp_path):
    from datetime import date, datetime, timedelta
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.models import Direction, Signal
    from quanti.risk.protections import ProtectionConfig, ProtectionManager
    from quanti.risk.protection_context import evaluate_entry

    class _OkRisk:
        def check(self, signal, portfolio):
            return True, ""

    class _NoRisk:
        def check(self, signal, portfolio):
            return False, "cap hit"

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.ensure_portfolio(100_000)
    today = date.today()

    def _iso(d):
        return datetime(d.year, d.month, d.day, 15, 0).isoformat()

    for i, c in enumerate(["000001", "000002", "000003"]):
        d = today - timedelta(days=i + 1)
        db.insert_order({
            "order_id": f"o{i}", "code": c, "direction": "sell",
            "quantity": 100, "price_type": "market", "limit_price": 0.0,
            "status": "filled", "strategy_name": "risk_exit",
            "filled_price": 9.0, "filled_quantity": 100,
            "reason": "止损 -10% ≤ -8%", "created_at": _iso(d),
            "filled_at": _iso(d), "entry_strategy": "",
        })
    pm = ProtectionManager(ProtectionConfig(
        sg_lookback_days=10, sg_trade_limit=3, sg_lock_days=10,
        max_drawdown_enabled=False))
    provider = DataProvider(db)
    buy = Signal("600519", Direction.BUY, 1.0, "buy")
    sell = Signal("600519", Direction.SELL, 1.0, "sell")

    ok, reason, kind = evaluate_entry(_OkRisk(), pm, db, provider, buy, object())
    assert ok is False and kind == "protection_block" and "StoplossGuard" in reason
    ok2, _, _ = evaluate_entry(_OkRisk(), pm, db, provider, sell, object())
    assert ok2 is True
    ok3, _, kind3 = evaluate_entry(_NoRisk(), pm, db, provider, buy, object())
    assert ok3 is False and kind3 == "risk_reject"
