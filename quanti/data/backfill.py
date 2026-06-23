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
    akshare backfill would be unworkable)."""
    from quanti.data.source import make_quote_adapter, make_stock_list_adapter

    end = end or date.today()
    start = end - timedelta(days=365 * years)
    # Roster first so the universe / point-in-time see delisted names too.
    make_stock_list_adapter(db, source, allow_fallback=False).sync_stock_list()
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
            n = adapter.sync_daily_quotes_by_date(d, seed_state=seed_state)
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
