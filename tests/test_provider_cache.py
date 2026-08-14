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


@pytest.fixture
def db_fund(tmp_path):
    """Bars + daily_basic + financials so the table caches are testable."""
    d = Database(str(tmp_path / "f.db"))
    d.initialize()
    days = [d.date() for d in pd.bdate_range("2024-01-02", periods=30)]
    for code in ("000001", "000002"):
        px = [10.0 + i * 0.01 for i in range(30)]
        d.save_daily_quotes(pd.DataFrame({
            "code": code, "date": days, "open": px, "high": [p + 0.1 for p in px],
            "low": [p - 0.1 for p in px], "close": px,
            "volume": [1e6] * 30, "amount": [p * 1e6 for p in px],
            "turnover": [1.0] * 30, "adj_factor": [1.0] * 30,
        }))
        d.save_daily_basic(pd.DataFrame({
            "code": code, "date": days,
            "pe": [10.0] * 30, "pe_ttm": [11.0] * 30, "pb": [1.0] * 30,
            "ps": [2.0] * 30, "ps_ttm": [2.0] * 30, "total_mv": [1e9] * 30,
            "circ_mv": [8e8] * 30, "dv_ratio": [0.5] * 30,
            "turnover_rate": [1.0] * 30,
        }))
    d.save_financials(pd.DataFrame({
        "code": ["000001", "000001", "000002"],
        "end_date": ["2024-03-31", "2024-06-30", "2024-03-31"],
        "ann_date": ["2024-04-20", "2024-07-25", "2024-04-21"],
        "report_type": ["1", "1", "1"],
        "roe": [5.0, 6.0, 7.0], "net_profit": [100.0, 110.0, 200.0],
        "revenue": [1000.0, 1100.0, 2000.0],
        "netprofit_yoy": [10.0, 12.0, 20.0], "revenue_yoy": [8.0, 9.0, 15.0],
    }))
    yield d
    d.close()


def test_basic_cache_hit_and_window_slice(db_fund):
    provider = DataProvider(db_fund, cache_ttl_sec=60.0)
    calls = {"n": 0}
    orig = db_fund.get_daily_basic

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    db_fund.get_daily_basic = counting
    start, end = date(2024, 1, 2), date(2024, 1, 20)
    df1 = provider.get_daily_basic_df("000001", start, end)
    df2 = provider.get_daily_basic_df("000001", start, end)
    assert calls["n"] == 1  # full table fetched once, sliced per call
    assert len(df1) == len(df2) == 14  # business days in [1/2, 1/19]


def test_financials_asof_pit_filter_from_cached_table(db_fund):
    provider = DataProvider(db_fund, cache_ttl_sec=60.0)
    calls = {"n": 0}
    orig = db_fund.get_financials

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    db_fund.get_financials = counting
    early = provider.get_financials_asof("000001", date(2024, 4, 30))
    late = provider.get_financials_asof("000001", date(2024, 8, 1))
    assert calls["n"] == 1  # one full-table fetch serves every as_of
    assert list(early["ann_date"]) == [date(2024, 4, 20)]
    assert list(late["ann_date"]) == [date(2024, 4, 20), date(2024, 7, 25)]


def test_basic_fin_invalidate_shows_fresh_rows(db_fund):
    provider = DataProvider(db_fund, cache_ttl_sec=60.0)
    provider.get_financials_asof("000002", date(2024, 5, 1))
    provider.get_daily_basic_df("000002", date(2024, 1, 2), date(2024, 1, 10))
    assert "000002" in provider._fin_cache
    assert "000002" in provider._basic_cache
    # a new report lands after the cache was filled
    db_fund.save_financials(pd.DataFrame({
        "code": ["000002"], "end_date": ["2024-06-30"],
        "ann_date": ["2024-07-26"], "report_type": ["1"],
        "roe": [8.0], "net_profit": [220.0], "revenue": [2200.0],
        "netprofit_yoy": [22.0], "revenue_yoy": [16.0],
    }))
    provider.invalidate_series_cache("000002")
    late = provider.get_financials_asof("000002", date(2024, 8, 1))
    assert len(late) == 2  # stale entry dropped, new report visible now


def test_basic_fin_results_are_copies(db_fund):
    provider = DataProvider(db_fund, cache_ttl_sec=60.0)
    a = provider.get_financials_asof("000001", date(2024, 8, 1))
    a["roe"] = -999.0  # caller mutation must not corrupt the cached entry
    b = provider.get_financials_asof("000001", date(2024, 8, 1))
    assert float(b["roe"].iloc[0]) == 5.0



