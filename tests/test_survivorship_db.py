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
