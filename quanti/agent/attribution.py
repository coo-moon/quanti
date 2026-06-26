"""Active-vs-passive attribution guardrail.

quanti's own rigorous full-market research concluded the active agent
(strategy fusion / factor selection) does NOT beat a passive equal-weight book
net of cost — beta (which pool) dominates within-pool alpha. This makes that
finding a LIVE, machine-readable signal instead of a one-time research note:
each day, compare the account's realized return over a trailing window against
equal-weighting the SAME candidate pool over the SAME dates. When the active
account lags the passive baseline, log it loudly so the operator sees they are
underperforming the basket they could have just held.

Observe-only: this never changes a trade. It is the cheap first step of the
"gate any active deviation against buy-and-hold" idea — surface first, enforce
later if desired.

Caveat (honest): `active` is the whole account (includes cash drag and legacy
holdings), `passive` is equal-weight of *today's* candidate pool over the
realized window — a fair "your account vs equal-weighting your pool" guardrail,
not an exact attribution. Fail-open (returns None) on thin history.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def active_vs_passive(db, provider, candidates: list[str], *,
                      window_days: int = 20, today: date | None = None,
                      min_names: int = 5, lag_margin: float = 0.0) -> dict | None:
    """Compare the account's realized return vs equal-weighting `candidates`
    over the same realized window. Returns a dict (or None if not enough data):

      {active, passive, gap, lagging, n_names, start, end, days}

    `gap = active - passive`; `lagging` is True when active < passive - lag_margin.
    """
    today = today or date.today()
    # Calendar lower bound generously covering `window_days` trading days.
    lower = today - timedelta(days=window_days * 2 + 7)

    # --- active: portfolio total-value change over the trailing window -------
    snaps: list[tuple[date, float]] = []
    for s in db.get_portfolio_snapshots(limit=400):
        try:
            sd = date.fromisoformat(s["snapshot_date"])
        except (ValueError, TypeError, KeyError):
            continue
        if lower <= sd <= today:
            snaps.append((sd, float(s["total_value"])))
    snaps.sort()
    if len(snaps) < 2:
        return None
    start, end = snaps[0][0], snaps[-1][0]
    v0, v1 = snaps[0][1], snaps[-1][1]
    if v0 <= 0 or start >= end:
        return None
    active = v1 / v0 - 1.0

    # --- passive: equal-weight candidate return over the SAME [start, end] ----
    rets: list[float] = []
    for code in candidates:
        bars = provider.get_daily_bars(code, start, end)
        if len(bars) >= 2 and bars[0].close > 0:
            rets.append(bars[-1].close / bars[0].close - 1.0)
    if len(rets) < min_names:
        return None
    passive = sum(rets) / len(rets)

    gap = active - passive
    return {
        "active": active, "passive": passive, "gap": gap,
        "lagging": active < passive - lag_margin,
        "n_names": len(rets), "start": start.isoformat(),
        "end": end.isoformat(), "days": (end - start).days,
    }
