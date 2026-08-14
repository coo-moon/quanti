"""Tests for quanti.health (doctor checks) + the once-a-day degraded-exit
log dedupe in quanti.execution.exits."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.execution import exits
from quanti.health import (
    data_freshness,
    db_integrity,
    exit_coverage,
    format_doctor,
    run_doctor,
)


@pytest.fixture
def db(tmp_path):
    """Two stocks with bars through yesterday + a calendar; a third stock
    with no bars (the 'never synced' case)."""
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    d.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    d.upsert_stock("600519", "贵州茅台", "SH", date(2001, 8, 27), "食品饮料")
    d.upsert_stock("000002", "万科A", "SZ", date(1991, 1, 29), "房地产")
    days = [date.today() - timedelta(days=2), date.today() - timedelta(days=1)]
    d.save_trade_calendar(days)
    rows = []
    for code in ("000001", "600519"):
        for i, day in enumerate(days):
            rows.append({
                "code": code, "date": day,
                "open": 10.0 + i, "high": 10.5 + i, "low": 9.8 + i,
                "close": 10.2 + i, "volume": 1e6, "amount": 1e7,
                "turnover": 1.0,
            })
    d.save_daily_quotes(pd.DataFrame(rows))
    yield d
    d.close()


# ---------------------------------------------------------------- exit coverage
def test_exit_coverage_detects_retired_strategy(db):
    db.upsert_position("000001", 100, 10.0, 10.2,
                       date.today() - timedelta(days=1),
                       entry_strategy="gone_strategy")
    report = exit_coverage(db, "strategies")
    assert report["ok"] is False
    assert len(report["degraded"]) == 1
    assert report["degraded"][0]["code"] == "000001"
    assert report["degraded"][0]["entry_strategy"] == "gone_strategy"
    assert "000001" in report["detail"]


def test_exit_coverage_ok_for_loaded_strategy(db):
    db.upsert_position("000001", 100, 10.0, 10.2,
                       date.today() - timedelta(days=1),
                       entry_strategy="ma_cross")
    report = exit_coverage(db, "strategies")
    assert report["ok"] is True
    assert report["degraded"] == []


def test_exit_coverage_ignores_manual_positions(db):
    db.upsert_position("000001", 100, 10.0, 10.2,
                       date.today() - timedelta(days=1),
                       entry_strategy=None)
    report = exit_coverage(db, "strategies")
    assert report["ok"] is True


def test_exit_coverage_missing_dir_reports_not_ok(db):
    db.upsert_position("000001", 100, 10.0, 10.2,
                       date.today() - timedelta(days=1),
                       entry_strategy="ma_cross")
    report = exit_coverage(db, "/nonexistent/strategies_dir_xyz")
    assert report["ok"] is False
    assert "detail" in report


# ---------------------------------------------------------------- data freshness
def test_data_freshness_all_fresh(db):
    report = data_freshness(db, codes=["000001", "600519"])
    assert report["ok"] is True
    assert report["missing"] == 0
    assert report["stale"] == 0
    assert report["expected"] == (date.today() - timedelta(days=1)).isoformat()


def test_data_freshness_flags_missing_bars(db):
    report = data_freshness(db, codes=["000001", "000002"])
    assert report["ok"] is False
    assert report["missing"] == 1
    assert report["missing_sample"] == ["000002"]


def test_data_freshness_flags_stale_bars(db):
    stale_day = date.today() - timedelta(days=7)
    db.upsert_stock("600000", "浦发银行", "SH", date(1999, 11, 10), "银行")
    db.save_daily_quotes(pd.DataFrame([{
        "code": "600000", "date": stale_day,
        "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
        "volume": 1e6, "amount": 1e7, "turnover": 1.0,
    }]))
    report = data_freshness(db, codes=["600000"], max_stale_days=3)
    assert report["ok"] is False
    assert report["stale"] == 1
    assert report["stale_sample"][0]["code"] == "600000"


def test_data_freshness_defaults_to_all_stocks(db):
    report = data_freshness(db)
    assert report["total"] == 3
    assert report["missing"] == 1  # 000002 has no bars
    assert report["ok"] is False


def test_data_freshness_empty_calendar_skips(db, tmp_path):
    empty = Database(str(tmp_path / "empty.db"))
    empty.initialize()
    report = data_freshness(empty, codes=[])
    assert report["ok"] is True
    assert report["expected"] is None


# ---------------------------------------------------------------- db integrity
def test_db_integrity_single_file(db):
    report = db_integrity(db)
    assert report["ok"] is True
    assert report["schemas"] == ["main"]


def test_db_integrity_with_market_attach(tmp_path):
    d = Database(str(tmp_path / "acct.db"),
                 market_db_path=str(tmp_path / "market.db"))
    d.initialize()
    report = db_integrity(d)
    assert report["ok"] is True
    assert set(report["schemas"]) == {"main", "market"}
    d.close()


# ---------------------------------------------------------------- aggregate
def test_run_doctor_aggregates_ok_when_clean(db):
    report = run_doctor(db, codes=["000001", "600519"])
    assert report["ok"] is True
    assert set(report["checks"]) == {"exit_coverage", "data_freshness",
                                     "db_integrity"}


def test_run_doctor_fails_on_degraded_exit(db):
    db.upsert_position("000001", 100, 10.0, 10.2,
                       date.today() - timedelta(days=1),
                       entry_strategy="gone_strategy")
    report = run_doctor(db, codes=["000001", "600519"])
    assert report["ok"] is False
    assert report["checks"]["exit_coverage"]["ok"] is False
    assert report["checks"]["data_freshness"]["ok"] is True


def test_format_doctor_marks_problems(db):
    db.upsert_position("000001", 100, 10.0, 10.2,
                       date.today() - timedelta(days=1),
                       entry_strategy="gone_strategy")
    text = format_doctor(run_doctor(db, codes=["000001", "600519"]))
    assert "exit_coverage" in text


# ------------------------------------------------------- exits warning dedupe
class _DummyStrat:
    pass


def test_degraded_warning_fires_once_per_day(db, caplog):
    positions = [{"code": "000001", "entry_strategy": "gone_unique_xyz"}]
    strategies = {"ma_cross": _DummyStrat}
    key = ("gone_unique_xyz", "000001")
    exits._degraded_warned.pop(key, None)
    try:
        with caplog.at_level(logging.WARNING):
            exits.compute_strategy_exits(None, strategies, positions, db)
            exits.compute_strategy_exits(None, strategies, positions, db)
    finally:
        exits._degraded_warned.pop(key, None)
    warnings = [r for r in caplog.records if "gone_unique_xyz" in r.getMessage()]
    assert len(warnings) == 1


def test_degraded_warning_refires_on_new_day(db, caplog):
    positions = [{"code": "000001", "entry_strategy": "gone_unique_abc"}]
    strategies = {"ma_cross": _DummyStrat}
    key = ("gone_unique_abc", "000001")
    exits._degraded_warned[key] = date.today() - timedelta(days=1)
    try:
        with caplog.at_level(logging.WARNING):
            exits.compute_strategy_exits(None, strategies, positions, db)
    finally:
        exits._degraded_warned.pop(key, None)
    warnings = [r for r in caplog.records if "gone_unique_abc" in r.getMessage()]
    assert len(warnings) == 1



# ---------------------------------------------------------------- API wiring
class TestDoctorEndpoint:
    @pytest.mark.asyncio
    async def test_doctor_endpoint(self, db):
        from httpx import ASGITransport, AsyncClient

        from quanti.api.app import create_app
        from quanti.data.provider import DataProvider
        app = create_app(db=db, provider=DataProvider(db),
                         strategies_dir="strategies")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            r = await c.get("/api/doctor", params={"codes": "000001,600519"})
        assert r.status_code == 200
        body = r.json()
        assert set(body["checks"]) == {"exit_coverage", "data_freshness",
                                       "db_integrity"}
        assert body["ok"] is True  # two synced codes, no positions

    @pytest.mark.asyncio
    async def test_doctor_endpoint_flags_degraded_exit(self, db):
        from httpx import ASGITransport, AsyncClient

        from quanti.api.app import create_app
        from quanti.data.provider import DataProvider
        db.upsert_position("000001", 100, 10.0, 10.2,
                           date.today() - timedelta(days=1),
                           entry_strategy="gone_strategy")
        app = create_app(db=db, provider=DataProvider(db),
                         strategies_dir="strategies")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            r = await c.get("/api/doctor")
        assert r.json()["ok"] is False
        assert r.json()["checks"]["exit_coverage"]["ok"] is False

    @pytest.mark.asyncio
    async def test_agent_status_exposes_degraded_exits(self, db):
        from httpx import ASGITransport, AsyncClient

        from quanti.api.app import create_app
        from quanti.data.provider import DataProvider
        db.upsert_position("000001", 100, 10.0, 10.2,
                           date.today() - timedelta(days=1),
                           entry_strategy="gone_strategy")
        app = create_app(db=db, provider=DataProvider(db),
                         strategies_dir="strategies")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            r = await c.get("/api/agent/status")
        assert r.status_code == 200
        degraded = r.json()["degraded_exits"]
        assert any(d["code"] == "000001"
                   and d["entry_strategy"] == "gone_strategy" for d in degraded)

