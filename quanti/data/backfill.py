"""Bulk multi-year history backfill, by trading date — efficient + resumable.

Per-stock pulls cost ~2 calls/stock; the WHOLE market for one day is ~2 calls
(pro.daily + pro.daily_basic). adj_factor is reconstructed from `daily`'s
pre_close (see tushare_adapter.reconstruct_adj_factor), so the rate-limited
adj_factor endpoint (as low as 1/min) is NEVER called — `daily` is 500/min. A
5-year full-market backfill is thus ~2,500 calls (≈1,250 trading days × 2)
against the generous `daily` cap, naturally batched, resumable
(backfill_progress checkpoint), and throttled to the per-minute cap.

A caller-owned `seed_state` dict carries each stock's (raw_close, factor) from
day to day so the cumulative adj_factor splices forward without re-reading the
DB per stock per day.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    dates_done: int = 0
    dates_skipped: int = 0
    rows: int = 0
    errors: list[str] = field(default_factory=list)


def _trade_dates(db, start: date, end: date) -> list[date]:
    """Trading days in [start, end] from the calendar; weekday fallback if the
    calendar is empty (sync it first for holiday accuracy)."""
    cal = db.get_trade_dates(start, end)
    if cal:
        return sorted(cal)
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def run_backfill(db, *, years: int = 5, end: date | None = None,
                 source: str = "tushare", resume: bool = True,
                 calls_per_min: int = 400, sleep_fn=time.sleep,
                 on_progress=None) -> BackfillResult:
    """Backfill `years` of daily history for the whole market (incl. delisted),
    oldest→newest so an interrupted run leaves a contiguous prefix. Requires a
    by-date-capable source (tushare); no silent akshare fallback (per-stock
    akshare backfill would be unworkable).

    `calls_per_min` paces the per-minute cap of the binding endpoint (`daily` is
    50/min on low-points tokens, 500/min on higher tiers) — set it at/under your
    token's `daily` limit. Even if over-paced, the by-date sweep retries
    patiently (waits out the minute) so days aren't dropped."""
    from quanti.data.source import make_quote_adapter, make_stock_list_adapter

    end = end or date.today()
    start = end - timedelta(days=365 * years)
    # Roster (universe / point-in-time membership incl. delisted). SKIP if a
    # survivorship-free roster is already present (any delisted name on file) —
    # tushare's stock_basic is ~1/min even at high tiers (it's a one-shot full
    # dump; points barely raise it), so L/D/P each backfill would just add ~2 min
    # of patient waiting. Refresh explicitly with `quanti sync --stocks`.
    try:
        has_roster = any(getattr(s, "delist_date", None) for s in db.list_stocks())
    except Exception:  # noqa: BLE001
        has_roster = False
    if has_roster:
        logger.info("roster 已含退市股 → 跳过名册同步(刷新:quanti sync --stocks)")
    else:
        # CONFIGURED source (tushare's stock_basic carries delist_date too).
        # Patient (waits per-minute limits) + best-effort: the by-date `daily`
        # sweep returns every stock regardless, so a roster blip never blocks the
        # backfill and we never silently swap vendors. Free akshare roster:
        # `quanti sync --stocks --source akshare`.
        try:
            n_roster = make_stock_list_adapter(db, source).sync_stock_list(patient=True)
            logger.info("roster: %d 只(%s,含退市股)", n_roster, source or "tushare")
        except Exception as e:  # noqa: BLE001 - roster is metadata; quotes don't need it
            logger.warning("roster 同步失败跳过(%s)——逐日 daily 仍覆盖全市场;"
                           "可单独 `quanti sync --stocks`", e)
    adapter = make_quote_adapter(db, source, allow_fallback=False)
    if not hasattr(adapter, "sync_daily_quotes_by_date"):
        raise RuntimeError(f"source {source!r} has no by-date backfill path")

    # Clean source migration: a full backfill REPLACES history with `source`, so
    # purge any other-vendor bars up front. Otherwise the one-source-per-code
    # guard in save_daily_quotes would skip every existing (e.g. akshare) code
    # and the backfill couldn't overwrite them. Untagged '' rows are kept.
    purged = db.purge_other_source_quotes(source)
    if purged:
        logger.warning("数据源迁移:清除 %d 行非 %s 历史(单源一致)", purged, source)

    dates = _trade_dates(db, start, end)
    done = db.get_backfilled_dates() if resume else set()
    res = BackfillResult()
    # ~2 calls per date (daily + daily_basic) → seconds/date for the per-min cap.
    min_interval = (2.0 / calls_per_min) * 60.0 if calls_per_min > 0 else 0.0
    # Carried across days so adj_factor reconstruction never re-reads the DB.
    seed_state: dict = {}

    for i, d in enumerate(dates):
        if d.isoformat() in done:
            res.dates_skipped += 1
            continue
        try:
            n = adapter.sync_daily_quotes_by_date(d, seed_state=seed_state,
                                                  patient=True)
            db.mark_backfill_done(d, n)
            res.dates_done += 1
            res.rows += n
        except Exception as e:  # noqa: BLE001 - log + continue (resumable)
            logger.warning("backfill %s failed: %s", d, e)
            res.errors.append(f"{d}: {e}")
            seed_state.clear()  # re-seed from persisted DB after a gap
        if on_progress:
            on_progress(i + 1, len(dates), d, res)
        if min_interval:
            sleep_fn(min_interval)

    logger.info("backfill done: %d dates, %d rows, %d skipped, %d errors",
                res.dates_done, res.rows, res.dates_skipped, len(res.errors))
    return res
