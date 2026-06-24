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


def compute_peaks(db, positions: list[dict]) -> dict[str, float]:
    """Per-code highest high since buy_date (post-entry peak), for trailing TP."""
    peaks: dict[str, float] = {}
    for p in positions:
        bd = p.get("buy_date")
        if bd is None:
            continue
        hw = db.get_high_water(p["code"], bd)
        if hw is not None:
            peaks[p["code"]] = hw
    return peaks


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
                           positions: list[dict]) -> set[str]:
    """Replay each holding's owning entry-strategy over its recent bars; return
    codes whose latest bar emits a SELL. Defaults-only params (v1) — close
    enough for an exit gate, and never raises into the cycle."""
    out: set[str] = set()
    if not strategies:
        return out
    end = date.today()
    start = end - timedelta(days=400)
    for p in positions:
        name = p.get("entry_strategy") or ""
        strat_cls = strategies.get(name)
        if strat_cls is None:
            continue
        try:
            bars = provider.get_daily_bars(p["code"], start, end)
            if not bars:
                continue
            strat = strat_cls()
            strat.init(getattr(strat, "params", {}) or {})
            last_signals: list = []
            for bar in bars:
                last_signals = strat.on_bar(bar) or []
            if any(s.direction == Direction.SELL
                   and s.stock_code == p["code"] for s in last_signals):
                out.add(p["code"])
        except Exception as e:  # noqa: BLE001 - one bad code can't stop exits
            logger.debug("strategy-exit replay skipped for %s/%s: %s",
                         p["code"], name, e)
    return out
