"""Unified data provider interface."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import date, timedelta

import pandas as pd

from quanti.data.database import Database
from quanti.models import BarData

#: Cache fetch window: bars from this date forward cover every hot path
#: (selector walk-forward full-history starts ~2021 in this deployment; the
#: gate's 730-day window; panel lookbacks). Older history is not cached.
_SERIES_CACHE_FROM = date(2015, 1, 1)


class DataProvider:
    """Reads market data from database. Single source of truth for strategies.

    `daily_quotes` stores RAW (不复权) prices + a per-(code,date) back-adjustment
    factor `adj_factor`. This is the single boundary where adjustment is applied:
    with ``adjust="hfq"`` (the default) prices are back-adjusted to a continuous
    series (correct returns, no ex-dividend gaps); with ``adjust="none"`` the raw
    market prices are returned (for live order pricing and UI display). The DB
    layer never adjusts — so gap-repair / re-reads can't double-adjust.
    """

    def __init__(self, db: Database, *,
                 cache_ttl_sec: float = 3600.0,
                 cache_max_codes: int = 500):
        """Args:
            cache_ttl_sec: per-code series cache lifetime. Hot loops (selector
                sweep, factor rescore, strategy gate) re-read the same bars
                tens of thousands of times per cold boot — without a cache
                each read takes the SQLite lock and the whole cold start
                becomes an hour-long lock convoy (2026-08-14 diagnosis).
                Measured 2026-08-14: at 60s the sweep's folds (which span
                minutes) expire the entries mid-run and every fold re-pierces
                the DB — the sweep went from 95s (quiet, cache-warm) to 7+ min
                live. 3600s covers the sweep; staleness is handled at the
                edges instead: freshness-critical reads pass fresh=True
                (next_trading_bar), sync writes invalidate (runtime + syncer),
                and realtime marks never touch this cache.
            cache_max_codes: LRU cap. ~500 codes x ~10 years of daily bars
                ≈ 40MB — bounded regardless of universe size.
        """
        self._db = db
        self._cache_ttl = cache_ttl_sec
        self._cache_max = cache_max_codes
        self._series_cache: OrderedDict[str, tuple[float, pd.DataFrame]] = (
            OrderedDict())
        # Same LRU/TTL design for the two per-code "table" families the factor
        # pipeline re-reads on every evaluation: daily_basic (PE/PB/mv/...) and
        # financials (PIT quarterly reports). The daily factor rescore alone
        # (118 factors x 2 windows x 100 codes) issued ~47k redundant SQLite
        # reads for these two tables — the 2026-08-15 stack dump showed those
        # queries dominating a 12-minute in-process mining hook. Full-history
        # per-code entries, same LRU cap as bars: at the 500-code cap the
        # daily_basic table is the dominant one (≈ a few tens of MB); the
        # financials table is ~37 rows/code and negligible.
        self._basic_cache: OrderedDict[str, tuple[float, pd.DataFrame]] = (
            OrderedDict())
        self._fin_cache: OrderedDict[str, tuple[float, pd.DataFrame]] = (
            OrderedDict())
        self._cache_lock = threading.Lock()

    def _cached_table(self, cache: "OrderedDict[str, tuple[float, pd.DataFrame]]",
                      code: str, fetch) -> pd.DataFrame:
        """Shared LRU/TTL get-or-fill for the per-code table caches.
        `fetch` is a zero-arg closure that reads the full per-code table from
        the DB once; callers slice/filter on top (never mutate the entry)."""
        now = time.monotonic()
        with self._cache_lock:
            hit = cache.get(code)
            if hit is not None and now - hit[0] < self._cache_ttl:
                cache.move_to_end(code)
                return hit[1]
        df = fetch()
        with self._cache_lock:
            cache[code] = (now, df)
            cache.move_to_end(code)
            while len(cache) > self._cache_max:
                cache.popitem(last=False)
        return df

    def _cached_series(self, code: str) -> pd.DataFrame:
        """RAW (unadjusted) bars for `code` from _SERIES_CACHE_FROM to today,
        served from an LRU/TTL cache. Callers slice + adjust on top."""
        return self._cached_table(
            self._series_cache, code,
            lambda: self._db.get_daily_quotes(
                code, _SERIES_CACHE_FROM, date.today() + timedelta(days=1)))

    def _cached_basic(self, code: str) -> pd.DataFrame:
        """Full daily_basic history for `code` (from _SERIES_CACHE_FROM)."""
        return self._cached_table(
            self._basic_cache, code,
            lambda: self._db.get_daily_basic(
                code, _SERIES_CACHE_FROM, date.today() + timedelta(days=1)))

    def _cached_financials(self, code: str) -> pd.DataFrame:
        """Full announced financial-report history for `code`."""
        return self._cached_table(
            self._fin_cache, code, lambda: self._db.get_financials(code))

    def invalidate_series_cache(self, code: str | None = None) -> None:
        """Drop one code (or everything) from all series caches — callers that
        just wrote bars / daily_basic / financials can force a fresh read
        without waiting out the TTL."""
        with self._cache_lock:
            for cache in (self._series_cache, self._basic_cache, self._fin_cache):
                if code is None:
                    cache.clear()
                else:
                    cache.pop(code, None)

    @staticmethod
    def _apply_adjust(df: pd.DataFrame, adjust: str) -> pd.DataFrame:
        """Back-adjust (hfq) a raw-price frame by adj_factor, or return raw.

        adjusted price = raw × adj_factor; volume = raw / adj_factor (Qlib
        convention, keeps price×volume ≈ amount); amount/turnover unchanged.
        Returns a copy — never mutates the input — so no double-adjustment.
        """
        if adjust == "none" or df.empty or "adj_factor" not in df.columns:
            return df
        df = df.copy()
        f = df["adj_factor"].astype(float)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] * f
        df["volume"] = df["volume"] / f
        return df

    def _windowed(self, code: str, start: date, end: date,
                   adjust: str, fresh: bool = False) -> pd.DataFrame:
        """Cached raw series sliced to [start, end] and adjusted. The TTL cache
        stores the RAW series (adjust is per-call — hfq and raw callers share
        the same entry). `fresh=True` (freshness checks: has the bar landed
        yet?) bypasses the cache AND drops the stale entry — a just-synced
        bar must be visible immediately, not after the TTL."""
        if fresh:
            with self._cache_lock:
                self._series_cache.pop(code, None)
            df = self._db.get_daily_quotes(code, start, end)
            return self._apply_adjust(df, adjust)
        full = self._cached_series(code)
        df = full[(full["date"] >= start) & (full["date"] <= end)]
        return self._apply_adjust(df, adjust)

    def get_daily_bars(self, code: str, start: date, end: date,
                       adjust: str = "hfq",
                       fresh: bool = False) -> list[BarData]:
        """Get daily bars. ``adjust="hfq"`` (default) returns back-adjusted
        prices; ``adjust="none"`` returns raw market prices."""
        df = self._windowed(code, start, end, adjust, fresh=fresh)
        return [
            BarData(
                code=row["code"],
                date=row["date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row["amount"],
                turnover=row.get("turnover", 0),
            )
            for _, row in df.iterrows()
        ]

    def get_daily_df(self, code: str, start: date, end: date,
                    adjust: str = "hfq",
                    fresh: bool = False) -> pd.DataFrame:
        """Get daily data as DataFrame (for factor computation). Adjusted (hfq)
        by default; pass ``adjust="none"`` for raw prices."""
        return self._windowed(code, start, end, adjust, fresh=fresh)

    def get_daily_basic_df(self, code: str, start: date, end: date) -> pd.DataFrame:
        """Per-(code,date) valuation (PE/PB/PS/mv/dv/turnover). Point-in-time by
        construction — used by the factor panel. Served from the per-code table
        cache (full history, sliced here) — a copy, safe to merge/mutate."""
        full = self._cached_basic(code)
        return full[(full["date"] >= start) & (full["date"] <= end)].copy()

    def get_financials_asof(self, code: str, as_of: date) -> pd.DataFrame:
        """Financial reports ANNOUNCED on/before `as_of` (ann_date ≤ as_of) —
        the point-in-time-safe view for fundamental factors. Served from the
        per-code table cache (full history, filtered here) — a copy, safe to
        merge/mutate."""
        full = self._cached_financials(code)
        return full[full["ann_date"] <= as_of].copy()

    def get_trade_dates(self, start: date, end: date) -> list[date]:
        return self._db.get_trade_dates(start, end)

    def is_trade_date(self, d: date) -> bool:
        return self._db.is_trade_date(d)

    def get_all_codes(self) -> list[str]:
        """Get all stock codes."""
        return [s.code for s in self._db.list_stocks()]

    def get_adv20_map(self, start: date, end: date,
                      window: int = 20) -> dict[str, float]:
        """20-day ADV per code over [start, end], batched in one query.

        See Database.get_adv20_map — used by `sort_by_adv20` to rank a whole
        universe without one round-trip per code.
        """
        return self._db.get_adv20_map(start, end, window)
