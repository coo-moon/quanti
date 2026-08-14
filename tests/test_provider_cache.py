"""Tests for DataProvider series cache (LRU + TTL)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "c.db"))
    d.initialize()
    days = [d.date() for d in pd.bdate_range("2024-01-02", periods=60)]
    for code in ("000001", "000002", "000003"):
        px = [10.0 + i * 0.01 for i in range(60)]
        d.save_daily_quotes(pd.DataFrame({
            "code": code, "date": days, "open": px, "high": [p + 0.1 for p in px],
            "low": [p - 0.1 for p in px], "close": px,
            "volume": [1e6] * 60, "amount": [p * 1e6 for p in px],
            "turnover": [1.0] * 60, "adj_factor": [1.0] * 60,
        }))
    yield d
    d.close()


def test_cache_hit_avoids_db_reads(db):
    provider = DataProvider(db, cache_ttl_sec=60.0)
    calls = {"n": 0}
    orig = db.get_daily_quotes

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    db.get_daily_quotes = counting
    start, end = date(2024, 1, 2), date(2024, 3, 1)
    df1 = provider.get_daily_df("000001", start, end)
    df2 = provider.get_daily_df("000001", start, end)
    assert calls["n"] == 1  # second read served from cache
    assert len(df1) == len(df2) == 44


def test_cache_window_slice_matches_direct_read(db):
    provider = DataProvider(db, cache_ttl_sec=60.0)
    start, end = date(2024, 1, 10), date(2024, 2, 10)
    cached = provider.get_daily_df("000001", start, end)
    direct = DataProvider(db, cache_ttl_sec=0.0).get_daily_df("000001", start, end)
    assert list(cached["date"]) == list(direct["date"])
    assert list(cached["close"]) == list(direct["close"])


def test_cache_ttl_zero_refetches(db):
    provider = DataProvider(db, cache_ttl_sec=0.0)
    calls = {"n": 0}
    orig = db.get_daily_quotes

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    db.get_daily_quotes = counting
    start, end = date(2024, 1, 2), date(2024, 3, 1)
    provider.get_daily_df("000001", start, end)
    provider.get_daily_df("000001", start, end)
    assert calls["n"] == 2


def test_cache_lru_eviction(db):
    provider = DataProvider(db, cache_ttl_sec=60.0, cache_max_codes=2)
    start, end = date(2024, 1, 2), date(2024, 3, 1)
    for code in ("000001", "000002", "000003"):
        provider.get_daily_df(code, start, end)
    assert len(provider._series_cache) == 2
    assert "000001" not in provider._series_cache  # oldest evicted


def test_invalidate_forces_refetch(db):
    provider = DataProvider(db, cache_ttl_sec=60.0)
    calls = {"n": 0}
    orig = db.get_daily_quotes

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    db.get_daily_quotes = counting
    start, end = date(2024, 1, 2), date(2024, 3, 1)
    provider.get_daily_df("000001", start, end)
    provider.invalidate_series_cache("000001")
    provider.get_daily_df("000001", start, end)
    assert calls["n"] == 2


def test_adjust_is_applied_per_call(db):
    provider = DataProvider(db, cache_ttl_sec=60.0)
    start, end = date(2024, 1, 2), date(2024, 3, 1)
    hfq = provider.get_daily_df("000001", start, end, adjust="hfq")
    raw = provider.get_daily_df("000001", start, end, adjust="none")
    assert list(hfq["close"]) == list(raw["close"])  # adj_factor=1 here
    assert id(hfq) != id(raw)  # never mutate the cached series



def test_fresh_read_bypasses_and_invalidates_cache(db):
    provider = DataProvider(db, cache_ttl_sec=60.0)
    calls = {"n": 0}
    orig = db.get_daily_quotes

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    db.get_daily_quotes = counting
    start, end = date(2024, 1, 2), date(2024, 3, 1)
    provider.get_daily_df("000001", start, end)  # warms the cache
    assert calls["n"] == 1
    df = provider.get_daily_df("000001", start, end, fresh=True)
    assert calls["n"] == 2  # fresh bypassed the cache
    assert len(df) == 44
    assert "000001" not in provider._series_cache  # entry dropped
    provider.get_daily_df("000001", start, end)  # refetches after drop
    assert calls["n"] == 3

