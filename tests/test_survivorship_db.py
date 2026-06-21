"""Tests for the stocks.delist_date column: round-trip, COALESCE-preserve, migration."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from quanti.data.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


def test_upsert_and_get_carries_delist_date(db):
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    db.upsert_stock("600001", "退市股", "SH", date(1999, 1, 1), "",
                    delist_date=date(2020, 5, 1))

    listed = db.get_stock("000001")
    delisted = db.get_stock("600001")
    assert listed is not None and listed.delist_date is None
    assert delisted is not None and delisted.delist_date == date(2020, 5, 1)


def test_list_stocks_carries_delist_date(db):
    db.upsert_stock("600001", "退市股", "SH", date(1999, 1, 1), "",
                    delist_date=date(2020, 5, 1))
    by_code = {s.code: s for s in db.list_stocks()}
    assert by_code["600001"].delist_date == date(2020, 5, 1)


def test_upsert_without_delist_date_preserves_existing(db):
    # Tushare sets the delist_date; a later AkShare upsert (no delist_date)
    # must NOT wipe it back to NULL — that's the COALESCE guard.
    db.upsert_stock("600001", "退市股", "SH", date(1999, 1, 1), "",
                    delist_date=date(2020, 5, 1))
    db.upsert_stock("600001", "退市股改名", "SH", date(1999, 1, 1), "综合")
    s = db.get_stock("600001")
    assert s.name == "退市股改名"
    assert s.delist_date == date(2020, 5, 1)  # preserved, not nulled


def test_legacy_db_migrates_delist_date(tmp_path):
    # A DB created before delist_date existed: stocks table without the column.
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE stocks (code TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "exchange TEXT NOT NULL, list_date TEXT NOT NULL, industry TEXT DEFAULT '')"
    )
    raw.execute("INSERT INTO stocks VALUES ('000001','平安银行','SZ','1991-04-03','银行')")
    raw.commit()
    raw.close()

    d = Database(path)
    d.initialize()  # _migrate must ADD COLUMN delist_date
    try:
        cols = [r[1] for r in d.conn.execute("PRAGMA table_info(stocks)").fetchall()]
        assert "delist_date" in cols
        s = d.get_stock("000001")
        assert s is not None and s.delist_date is None  # legacy row reads as listed
    finally:
        d.close()


def test_get_pool_stocks_carries_delist_date(db):
    db.upsert_stock("600001", "退市", "SH", date(2000, 1, 1), "",
                    delist_date=date(2019, 1, 1))
    db.create_pool("p1")
    db.add_stocks_to_pool("p1", ["600001"])
    pooled = db.get_pool_stocks("p1")
    assert len(pooled) == 1
    assert pooled[0].delist_date == date(2019, 1, 1)


def test_point_in_time_universe(db):
    # listed before window, never delisted → included
    db.upsert_stock("000001", "在市", "SZ", date(2010, 1, 1), "")
    # delisted mid-window → included (it traded during the window)
    db.upsert_stock("600001", "中途退市", "SH", date(2005, 1, 1), "",
                    delist_date=date(2022, 6, 1))
    # delisted BEFORE window start → excluded
    db.upsert_stock("600002", "早已退市", "SH", date(2000, 1, 1), "",
                    delist_date=date(2019, 1, 1))
    # listed AFTER window end → excluded
    db.upsert_stock("301001", "窗后上市", "SZ", date(2023, 1, 1), "")
    # listed mid-window, still listed → included (existed for part of window)
    db.upsert_stock("000002", "窗中上市", "SZ", date(2021, 6, 1), "")

    universe = db.point_in_time_universe(date(2021, 1, 1), date(2022, 12, 31))
    assert universe == ["000001", "000002", "600001"]
