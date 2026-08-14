"""Shared exit-input computation for the live/paper brokers.

Both PaperBroker and QmtBroker feed ``RiskManager.check_exits`` the same two
inputs — each holding's post-entry peak (for the trailing take-profit) and the
set of codes whose owning strategy now says SELL. Keeping that computation here
stops paper and live from drifting apart, which is the whole point of routing
exits through one RiskManager. Both read the local DB mirror (buy_date /
entry_strategy live there, not on the venue account), so the result is keyed by
code: a code missing from the mirror simply degrades to plain stop-loss.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from quanti.models import Direction

logger = logging.getLogger(__name__)

# (strategy_name, code) -> last calendar day the degradation warning fired.
# The intraday guard calls check_exits every few seconds, so without this the
# log floods with the same warning thousands of times a day (observed on a
# live paper book: ~5s cadence × 3 degraded positions ≈ 50k lines/day).
_degraded_warned: dict[tuple[str, str], date] = {}


def _warn_degraded_once(name: str, code: str) -> None:
    """Warn about a holding whose owning strategy is missing — at most once
    per (strategy, code) per calendar day. The set is bounded by the book's
    degraded holdings, not by the call rate."""
    key = (name, code)
    today = date.today()
    if _degraded_warned.get(key) == today:
        return
    _degraded_warned[key] = today
    logger.warning(
        "entry_strategy %r for %s not loaded — strategy exit "
        "degraded to stop-loss/TP only (warned once today)", name, code)


def compute_peaks(db, positions: list[dict],
                  raw_axis: bool = False) -> dict[str, float]:
    """Per-code highest high since buy_date (post-entry peak), for trailing TP.

    ``get_high_water`` returns the peak on the hfq axis — right for
    PaperBroker, whose current_price/avg_cost come off the provider's hfq
    default. Live venue prices are raw: pass ``raw_axis=True`` to divide each
    peak by the code's latest adj_factor, re-expressing it on today's raw
    axis so (current - peak)/peak stays a return-space retrace across
    ex-dividend gaps."""
    peaks: dict[str, float] = {}
    for p in positions:
        bd = p.get("buy_date")
        if bd is None:
            continue
        hw = db.get_high_water(p["code"], bd)
        if hw is None:
            continue
        if raw_axis:
            q = db.get_latest_quote_before(p["code"],
                                           date.today() + timedelta(days=1))
            if q is None or q[1] <= 0:
                continue
            hw /= q[1]
        peaks[p["code"]] = hw
    return peaks


def compute_atr_ratios(provider, positions: list[dict],
                       n: int) -> dict[str, float]:
    """Per-code ATR(n)/latest_close — the dimensionless volatility ratio the
    ATR-adaptive stop needs. A ratio is adjust-agnostic (hfq vs raw give the
    same number), matching the backtest, and comparable to pos.pnl_pct. Uses
    only recent history (ATR is a current-volatility measure, not tied to
    buy_date). Never raises into the exit cycle."""
    import pandas as pd

    from quanti.factors.technical import compute_atr
    out: dict[str, float] = {}
    start = date.today() - timedelta(days=n * 4 + 40)  # enough bars to warm ATR
    for p in positions:
        code = p["code"]
        try:
            bars = provider.get_daily_bars(code, start, date.today())
            if len(bars) < n + 1:
                continue
            df = pd.DataFrame({"high": [b.high for b in bars],
                               "low": [b.low for b in bars],
                               "close": [b.close for b in bars]})
            atr = compute_atr(df, n).iloc[-1]
            close = df["close"].iloc[-1]
            if atr == atr and close > 0:  # atr==atr filters NaN warm-up
                out[code] = float(atr / close)
        except Exception as e:  # noqa: BLE001 - one bad code can't stop exits
            logger.debug("ATR ratio skipped for %s: %s", code, e)
    return out


def load_strategies(strategies_dir: str) -> dict:
    """Load strategy classes by name. Returns {} if the loader/dir is
    unavailable, so exits degrade to stop-loss + take-profit only."""
    cache: dict = {}
    try:
        from quanti.strategy.loader import StrategyLoader
        for s in StrategyLoader().load_directory(strategies_dir):
            cache[s.name] = type(s)
    except Exception as e:  # noqa: BLE001 - never break the exit cycle
        logger.debug("strategy load for exits failed: %s", e)
    return cache


def compute_strategy_exits(provider, strategies: dict,
                           positions: list[dict], db) -> dict[str, str]:
    """Replay each holding's owning entry-strategy over its recent bars; return
    ``{code: owning-strategy-name}`` for holdings whose latest bar emits a SELL
    (the name lets the exit audit show WHICH strategy said sell). Replays with
    the strategy's ACTIVE params (``resolve_params`` — tuned-if-accepted layered
    over goal params, the same resolution the entry path uses), not bare class
    defaults, so the exit matches how the position was opened. Never raises into
    the cycle."""
    out: dict[str, str] = {}
    if not strategies:
        return out
    from quanti.agent.goal import load_goal
    from quanti.agent.params import resolve_params
    goal = load_goal(db)
    end = date.today()
    start = end - timedelta(days=400)
    for p in positions:
        name = p.get("entry_strategy") or ""
        strat_cls = strategies.get(name)
        if strat_cls is None:
            if name:
                # 归属策略已被移除(如精简进 attic)→ 该持仓失去策略离场,
                # 只剩止损/止盈。显式告警而非静默跳过(#96 教训);盘中守卫
                # 每几秒跑一次,按 (策略, 代码) 每日只告警一次防刷屏。
                _warn_degraded_once(name, p["code"])
            continue
        try:
            bars = provider.get_daily_bars(p["code"], start, end)
            if not bars:
                continue
            strat = strat_cls()
            strat.init(resolve_params(db, name, goal))
            last_signals: list = []
            for bar in bars:
                last_signals = strat.on_bar(bar) or []
            if any(s.direction == Direction.SELL
                   and s.stock_code == p["code"] for s in last_signals):
                out[p["code"]] = name
        except Exception as e:  # noqa: BLE001 - one bad code can't stop exits
            logger.debug("strategy-exit replay skipped for %s/%s: %s",
                         p["code"], name, e)
    return out
