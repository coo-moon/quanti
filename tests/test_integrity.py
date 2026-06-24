"""Tests for source-agnostic daily-quote completeness validation."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.integrity import (
    check_quote_completeness,
    expected_trading_days,
)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


def _save(db, code, days):
    db.save_daily_quotes(pd.DataFrame([
        {"code": code, "date": dd, "open": 10.0, "high": 10.5, "low": 9.8,
         "close": 10.2, "volume": 1e6, "amount": 1e7, "turnover": 1.0,
         "source": "tushare"} for dd in days]))


CAL = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
       date(2024, 1, 5)]  # a 4-day trading calendar


def test_full_coverage_is_clean(db):
    db.save_trade_calendar(CAL)
    _save(db, "000001", CAL)
    rep = check_quote_completeness(db, "000001", date(2024, 1, 1), date(2024, 1, 6))
    assert rep.expected == 4 and rep.present == 4
    assert rep.coverage == 1.0 and rep.clean
    assert rep.used_calendar is True


def test_missing_trading_day_counted_to_the_day(db):
    """A single missing trading day is caught (the old >15-day heuristic missed
    these)."""
    db.save_trade_calendar(CAL)
    _save(db, "000001", [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)])
    rep = check_quote_completeness(db, "000001", date(2024, 1, 1), date(2024, 1, 6))
    assert rep.present == 3 and rep.expected == 4
    assert rep.missing_days == [date(2024, 1, 4)]
    assert rep.coverage == pytest.approx(0.75) and not rep.clean


def test_empty_stored_all_missing(db):
    db.save_trade_calendar(CAL)
    rep = check_quote_completeness(db, "999999", date(2024, 1, 1), date(2024, 1, 6))
    assert rep.present == 0 and rep.missing_days == CAL


def test_weekday_fallback_when_calendar_unsynced(db):
    # No calendar saved → weekday fallback; 2024-01-01 is Mon (holiday IRL but
    # counted as a weekday here — that's the documented coarseness).
    _save(db, "000001", [date(2024, 1, 2), date(2024, 1, 3)])
    rep = check_quote_completeness(db, "000001", date(2024, 1, 1), date(2024, 1, 5))
    assert rep.used_calendar is False
    # Mon–Fri = 5 weekdays expected, 2 present.
    assert rep.expected == 5 and rep.present == 2


def test_quality_defects_counted(db):
    db.save_trade_calendar(CAL)
    df = pd.DataFrame([
        {"code": "000001", "date": date(2024, 1, 2), "open": 10, "high": 9,
         "low": 11, "close": 10, "volume": 1e6, "amount": 1e7, "turnover": 1,
         "source": "tushare"},   # high<low → bad OHLC
        {"code": "000001", "date": date(2024, 1, 3), "open": 0, "high": 10,
         "low": 8, "close": 9, "volume": 1e6, "amount": 1e7, "turnover": 1,
         "source": "tushare"},   # nonpositive open price only (OHLC order ok)
    ])
    db.save_daily_quotes(df)
    rep = check_quote_completeness(db, "000001", date(2024, 1, 1), date(2024, 1, 6))
    assert rep.bad_ohlc == 1
    assert rep.nonpos_price == 1
    assert not rep.clean


def test_expected_trading_days_prefers_calendar(db):
    db.save_trade_calendar(CAL)
    days, used = expected_trading_days(db, date(2024, 1, 1), date(2024, 1, 6))
    assert used is True and days == CAL


def test_summary_is_human_readable(db):
    db.save_trade_calendar(CAL)
    _save(db, "000001", [date(2024, 1, 2)])
    rep = check_quote_completeness(db, "000001", date(2024, 1, 1), date(2024, 1, 6))
    s = rep.summary()
    assert "覆盖" in s and "缺 3 个交易日" in s


# --- async sync surfacing (the layer this feature adds) -------------------

class _PartialAdapter:
    """Stub that stores a scripted set of dates per code (to simulate partial
    coverage), so we can assert the async job surfaces completeness."""

    def __init__(self, db, bars):   # bars: {code: [date, ...]}
        self._db = db
        self._bars = bars

    def sync_daily_quotes(self, code, start=None, end=None, repair_gaps=True,
                          with_basic=False):
        days = self._bars.get(code, [])
        if not days:
            return 0
        self._db.save_daily_quotes(pd.DataFrame([
            {"code": code, "date": d, "open": 10.0, "high": 10.5, "low": 9.8,
             "close": 10.2, "volume": 1e6, "amount": 1e7, "turnover": 1.0,
             "source": "tushare"} for d in days]))
        return len(days)


def _recent_cal():
    from datetime import timedelta
    today = date.today()
    return [today, today - timedelta(days=7), today - timedelta(days=14)]


def test_async_sync_surfaces_completeness_warnings(db, monkeypatch):
    import asyncio

    from quanti.api.routes import _run_quotes_sync
    from quanti.data import source as src

    cal = _recent_cal()
    db.save_trade_calendar(cal)
    adapter = _PartialAdapter(db, {"FULL": cal, "PARTIAL": cal[1:]})  # PARTIAL misses today
    monkeypatch.setattr(src, "try_make_quote_adapter", lambda _db: (adapter, None))
    db.create_sync_job("j1", "_quotes_sync", 2)
    asyncio.run(_run_quotes_sync("j1", ["FULL", "PARTIAL"], db))
    job = db.get_sync_job("j1")
    assert job["status"] == "done"            # gaps are warnings, not failures
    assert job["errors"] == {}
    assert "PARTIAL" in job["warnings"] and "FULL" not in job["warnings"]
    assert "缺 1 个交易日" in job["warnings"]["PARTIAL"]


def test_async_sync_min_coverage_fails_loud(db, monkeypatch):
    import asyncio

    from quanti.api.routes import _run_quotes_sync
    from quanti.data import source as src

    cal = _recent_cal()
    db.save_trade_calendar(cal)
    adapter = _PartialAdapter(db, {"PARTIAL": cal[1:]})   # 2/3 coverage
    monkeypatch.setattr(src, "try_make_quote_adapter", lambda _db: (adapter, None))
    db.create_sync_job("j2", "_quotes_sync", 1)
    asyncio.run(_run_quotes_sync("j2", ["PARTIAL"], db, min_coverage=0.9))
    job = db.get_sync_job("j2")
    assert job["status"] == "error"           # below threshold → hard error
    assert "PARTIAL" in job["errors"]
