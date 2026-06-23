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

    def sync_stock_list(self) -> int:
        return 0

    def sync_daily_quotes_by_date(self, d: date) -> int:
        self.seen.append(d)
        if d == date(2024, 1, 3):
            raise RuntimeError("boom")   # one transient failure
        return 5


@pytest.fixture
def fake_adapter(monkeypatch):
    a = _FakeAdapter()
    monkeypatch.setattr(src, "make_stock_list_adapter", lambda *x, **k: a)
    monkeypatch.setattr(src, "make_quote_adapter", lambda *x, **k: a)
    return a


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
    # ~3 calls/date at 600/min → 0.3s/date; called once per processed date.
    assert slept and all(s == pytest.approx(0.3) for s in slept)
