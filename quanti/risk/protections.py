# quanti/risk/protections.py
"""Composable, pluggable risk *protections* — a soft layer ABOVE the hard
RiskManager caps and BELOW the -15% portfolio breaker.

Each protection answers one question: "should new BUYs be locked today?".
They never force a sell. Pure logic: given a ProtectionContext of facts
(recent stop-loss exit dates, recent equity series, a trading-day-distance
function), decide locked/allowed. Live and backtest feed the same logic from
different fact sources — see protection_context.py (live) and
backtest/engine.py (in-memory).

Lock model (forward-K-lock, stateless): a day `e` is a "trigger day" when its
protection condition holds; today is locked iff some trigger day falls within
the last K trading days. This is fully derivable from facts (no persisted lock
object, restart-safe) yet gives freqtrade-style hysteresis — once tripped it
stays locked K trading days, and continued distress extends it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import numpy as np


@dataclass
class ProtectionConfig:
    """Tunable thresholds. All windows/locks are in trading days."""

    enabled: bool = True

    # StoplossGuard: >= sg_trade_limit stop-loss exits within sg_lookback_days
    # trading days → lock new BUYs for sg_lock_days trading days.
    stoploss_guard_enabled: bool = True
    sg_lookback_days: int = 5
    sg_trade_limit: int = 3
    sg_lock_days: int = 5

    # MaxDrawdown: window peak-to-trough drawdown over md_lookback_days trading
    # days <= md_max_drawdown_pct → lock new BUYs for md_lock_days trading days.
    # Must stay strictly shallower than the -0.15 hard portfolio breaker.
    max_drawdown_enabled: bool = True
    md_lookback_days: int = 10
    md_max_drawdown_pct: float = -0.08
    md_lock_days: int = 10
    md_min_points: int = 5  # fewer equity points in window → fail-open

    # CorrelationGuard (opt-in, default OFF — it changes live BUY behavior): when
    # the held book is already a highly-correlated single bet, lock new BUYs so
    # the agent doesn't pile more into the same exposure. Preventive only — never
    # forces a sell (the -15% breaker + ATR stops own the sell side). Targets the
    # known failure mode: an "equal-weight" book that is really one small-cap beta
    # with a fat left tail. Point-in-time on today's holdings (no K-lock history).
    correlation_guard_enabled: bool = False
    cg_lookback_days: int = 20      # trailing daily returns per holding
    cg_max_avg_corr: float = 0.75   # avg pairwise corr >= this → lock new BUYs
    cg_min_holdings: int = 5        # fewer holdings → fail-open (not a concentration risk)

    # UnlockGuard (opt-in, default OFF): block a BUY on a name with a large
    # share-unlock (限售解禁) within the horizon — supply shocks are a documented
    # A-share left-tail driver. Per-code (uses the `code` arg), unlike the global
    # guards. Risk overlay, never alpha. Fed by the share_unlocks table, which is
    # ONLY populated by calling AkShareAdapter.sync_share_unlocks — an empty
    # table (no fetch run yet) → fail-open. Default OFF so an unverified akshare
    # column/unit can't bite production until someone runs+verifies the fetch and
    # enables this. The pct denominator (流通股 vs 总股本) is whatever akshare's
    # column exposes — verify live before trusting the absolute threshold.
    unlock_guard_enabled: bool = False
    ug_horizon_days: int = 30        # look this many days ahead for unlocks
    ug_min_float_pct: float = 0.05   # unlock 占比 >= this fraction → block buy


@dataclass
class ProtectionContext:
    """Facts the protections read. Built from DB (live) or memory (backtest).

    `trading_days_between(start, end)` counts trading days in (start, end]
    (same contract as quanti.utils.market.count_trading_days_between)."""

    today: date
    stop_loss_exit_dates: list[date]
    equity_series: list[tuple[date, float]]
    trading_days_between: Callable[[date, date], int]
    # { code → trailing daily returns (chronological) } for currently-held
    # names; only populated when correlation_guard_enabled. Empty → guard
    # fail-opens (no concentration signal available).
    holdings_returns: dict[str, list[float]] = field(default_factory=dict)
    # { code → max float-fraction unlocking within ug_horizon_days }; only
    # populated when unlock_guard_enabled. Empty / code absent → fail-open.
    upcoming_unlocks: dict[str, float] = field(default_factory=dict)


class ProtectionManager:
    """Aggregates the enabled protections. First one that locks wins."""

    def __init__(self, config: ProtectionConfig | None = None) -> None:
        self.config = config or ProtectionConfig()

    def check_entry(self, ctx: ProtectionContext,
                    code: str | None = None) -> tuple[bool, str]:
        """Return (allowed, reason). allowed=False blocks a BUY. `code` is used
        only by the per-code UnlockGuard; the other guards are global."""
        if not self.config.enabled:
            return True, ""
        for reason in (self._stoploss_guard(ctx), self._max_drawdown(ctx),
                       self._correlation_guard(ctx),
                       self._unlock_guard(ctx, code)):
            if reason:
                return False, reason
        return True, ""

    # ------------------------------------------------------------------
    def _stoploss_guard(self, ctx: ProtectionContext) -> str | None:
        cfg = self.config
        if not cfg.stoploss_guard_enabled:
            return None
        dates = sorted(d for d in ctx.stop_loss_exit_dates if d <= ctx.today)
        if not dates:
            return None
        W, N, K = cfg.sg_lookback_days, cfg.sg_trade_limit, cfg.sg_lock_days
        latest_trigger: date | None = None
        for i, e in enumerate(dates):
            # Stops within the W-trading-day window ending at e (e included).
            count = sum(1 for d in dates[:i + 1]
                        if ctx.trading_days_between(d, e) < W)
            if count >= N and (latest_trigger is None or e > latest_trigger):
                latest_trigger = e
        if latest_trigger is None:
            return None
        if ctx.trading_days_between(latest_trigger, ctx.today) <= K:
            return (f"StoplossGuard 锁定: 近{W}交易日止损≥{N}次 "
                    f"(最近触发 {latest_trigger.isoformat()}, 锁{K}交易日)")
        return None

    def _max_drawdown(self, ctx: ProtectionContext) -> str | None:
        cfg = self.config
        if not cfg.max_drawdown_enabled:
            return None
        W, thr, K = (cfg.md_lookback_days, cfg.md_max_drawdown_pct,
                     cfg.md_lock_days)
        series = sorted((d, v) for d, v in ctx.equity_series if d <= ctx.today)
        if not series:
            return None
        latest_trigger: date | None = None
        for j, (d, _v) in enumerate(series):
            window = [(wd, wv) for wd, wv in series[:j + 1]
                      if ctx.trading_days_between(wd, d) < W]
            if len(window) < cfg.md_min_points:
                continue  # fail-open on thin window
            peak = None
            mdd = 0.0
            for _wd, wv in window:
                if peak is None or wv > peak:
                    peak = wv
                if peak > 0:  # guards a degenerate zero-valued equity point
                    dd = (wv - peak) / peak
                    if dd < mdd:
                        mdd = dd
            if mdd <= thr and (latest_trigger is None or d > latest_trigger):
                latest_trigger = d
        if latest_trigger is None:
            return None
        if ctx.trading_days_between(latest_trigger, ctx.today) <= K:
            return (f"MaxDrawdown 锁定: 近{W}交易日净值回撤≤{thr:.0%} "
                    f"(最近触发 {latest_trigger.isoformat()}, 锁{K}交易日)")
        return None

    def _correlation_guard(self, ctx: ProtectionContext) -> str | None:
        """Lock new BUYs when the held book's average pairwise return-correlation
        is too high — it's already effectively one bet. Fail-open on too few
        holdings or too little aligned history."""
        cfg = self.config
        if not cfg.correlation_guard_enabled:
            return None
        # Trailing returns per holding, trimmed to the lookback, kept only if
        # they have >= 2 points.
        series = [r[-cfg.cg_lookback_days:] for r in ctx.holdings_returns.values()
                  if r and len(r) >= 2]
        if len(series) < cfg.cg_min_holdings:
            return None
        n = min(len(s) for s in series)
        if n < 2:
            return None
        arr = np.asarray([s[-n:] for s in series], dtype=float)  # (k, n)
        # Drop holdings with zero variance over the window (corrcoef → NaN row).
        arr = arr[arr.std(axis=1) > 0]
        if arr.shape[0] < cfg.cg_min_holdings:
            return None
        cm = np.corrcoef(arr)
        iu = np.triu_indices(cm.shape[0], k=1)
        vals = cm[iu]
        vals = vals[~np.isnan(vals)]
        if vals.size == 0:
            return None
        avg = float(np.mean(vals))
        if avg >= cfg.cg_max_avg_corr:
            return (f"CorrelationGuard 锁定: {arr.shape[0]}只持仓近{n}日平均两两相关 "
                    f"{avg:.2f}≥{cfg.cg_max_avg_corr}(组合已是单一相关下注,暂停新买)")
        return None

    def _unlock_guard(self, ctx: ProtectionContext, code: str | None) -> str | None:
        """Block a BUY on `code` if it has a large share-unlock within the
        horizon. Per-code; fail-open when off, code missing, or no unlock data."""
        cfg = self.config
        if not cfg.unlock_guard_enabled or not code:
            return None
        pct = ctx.upcoming_unlocks.get(code)
        if pct is not None and pct >= cfg.ug_min_float_pct:
            return (f"UnlockGuard 锁定 {code}: 近{cfg.ug_horizon_days}日解禁占比 "
                    f"{pct:.1%}≥{cfg.ug_min_float_pct:.0%}(供给冲击左尾,暂停新买)")
        return None
