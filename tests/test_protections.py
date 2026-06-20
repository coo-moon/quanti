# tests/test_protections.py
from __future__ import annotations

from datetime import date, timedelta

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


# ---- Aggregation ------------------------------------------------------

def test_check_entry_first_lock_wins_and_disabled_passes():
    mgr = ProtectionManager(ProtectionConfig(enabled=False))
    eq = _equity([(0, 100), (1, 96), (2, 92), (3, 90), (4, 88)])
    assert mgr.check_entry(_ctx(_d(4), sl_dates=[_d(2), _d(3), _d(4)],
                                equity=eq)) == (True, "")
