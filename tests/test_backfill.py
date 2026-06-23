"""Tests for the by-trading-date bulk backfill driver (mocked, no network)."""

from __future__ import annotations

from datetime import date

import pytest

import quanti.data.source as src
from quanti.data.backfill import run_backfill
from quanti.data.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    # 3-day calendar so _trade_dates is deterministic (no weekday fallback).
    d.save_trade_calendar([date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    yield d
    d.close()


class _FakeAdapter:
    def __init__(self):
        self.seen: list[date] = []

    def sync_stock_list(self, patient: bool = False) -> int:
        return 0

    def sync_daily_quotes_by_date(self, d: date, seed_state=None,
                                  patient: bool = False) -> int:
        self.seen.append(d)
        if d == date(2024, 1, 3):
            raise RuntimeError("boom")   # one transient failure
        return 5


@pytest.fixture
def fake_adapter(monkeypatch):
    a = _FakeAdapter()
    # Roster goes through the configured source (tushare) again; quotes too.
    monkeypatch.setattr(src, "make_stock_list_adapter", lambda *x, **k: a)
    monkeypatch.setattr(src, "make_quote_adapter", lambda *x, **k: a)
    return a


def test_backfill_skips_roster_when_already_present(db, fake_adapter, monkeypatch):
    """A survivorship-free roster already on file (delisted names) → backfill
    skips the slow ~1/min stock_basic re-sync."""
    db.upsert_stock("600001", "邯郸钢铁", "SH", date(1998, 1, 22), "",
                    delist_date=date(2009, 12, 29))
    calls = {"n": 0}
    orig = fake_adapter.sync_stock_list
    monkeypatch.setattr(fake_adapter, "sync_stock_list",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         orig(*a, **k))[1])
    run_backfill(db, years=1, end=date(2024, 1, 4), source="tushare",
                 sleep_fn=lambda _x: None)
    assert calls["n"] == 0              # roster sync skipped


def test_backfill_runs_and_records_progress(db, fake_adapter):
    res = run_backfill(db, years=1, end=date(2024, 1, 4), source="tushare",
                       sleep_fn=lambda _x: None)
    assert res.dates_done == 2          # Jan 2 + Jan 4 succeeded
    assert res.rows == 10               # 2 dates × 5 rows
    assert len(res.errors) == 1         # Jan 3 failed
    # Only successful dates are checkpointed.
    assert db.get_backfilled_dates() == {"2024-01-02", "2024-01-04"}


def test_backfill_resume_skips_done(db, fake_adapter):
    run_backfill(db, years=1, end=date(2024, 1, 4), source="tushare",
                 sleep_fn=lambda _x: None)
    fake_adapter.seen.clear()
    res2 = run_backfill(db, years=1, end=date(2024, 1, 4), source="tushare",
                        sleep_fn=lambda _x: None)
    assert res2.dates_skipped == 2                  # Jan 2 + Jan 4 already done
    assert fake_adapter.seen == [date(2024, 1, 3)]  # only the failed day retried


def test_backfill_throttles(db, fake_adapter):
    slept: list[float] = []
    run_backfill(db, years=1, end=date(2024, 1, 4), source="tushare",
                 calls_per_min=600, sleep_fn=slept.append)
    # ~2 calls/date (daily + daily_basic; no adj_factor) at 600/min → 0.2s/date.
    assert slept and all(s == pytest.approx(0.2) for s in slept)


def test_backfill_purges_other_source_for_clean_migration(db, fake_adapter):
    """A full backfill is a source migration: pre-existing akshare bars are
    purged up front so the new vendor's history isn't blocked by the
    one-source-per-code guard. Untagged ('') rows are kept."""
    import pandas as pd
    db.save_daily_quotes(pd.DataFrame({
        "code": ["000001", "000001"], "date": [date(2023, 6, 1), date(2023, 6, 2)],
        "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0,
        "volume": 1e6, "amount": 1e7, "turnover": 1.0, "source": "akshare"}))
    assert db.get_quote_source("000001") == "akshare"
    run_backfill(db, years=1, end=date(2024, 1, 4), source="tushare",
                 sleep_fn=lambda _x: None)
    # akshare history is gone; the guard would now accept tushare for 000001.
    assert db.get_quote_source("000001") is None
    assert len(db.get_daily_quotes("000001", date(2023, 1, 1),
                                   date(2024, 1, 9))) == 0
