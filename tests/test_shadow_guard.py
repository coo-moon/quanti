"""Startup guard against the recurring shadow-table masking bug.

When a market DB is attached, a shared-table copy in the account (main) DB
shadows the attached `market.*` (SQLite resolves unqualified names to main
first) → unqualified reads silently return the empty/stale account copy
("数据没了"). The guard drops EMPTY shadows automatically and loudly flags
non-empty ones (never silently deletes possible user data).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from quanti.data.database import Database


def _market_with_one_stock(path) -> None:
    mdb = Database(str(path))          # single-file → tables in this file's main
    mdb.initialize()
    mdb.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    mdb.close()


def test_empty_shadow_dropped_read_falls_through_to_market(tmp_path):
    market, acct = tmp_path / "market.db", tmp_path / "acct.db"
    _market_with_one_stock(market)
    # Seed an EMPTY shadow `stocks` in the account DB (before any market attach).
    raw = sqlite3.connect(str(acct))
    raw.execute("CREATE TABLE stocks (code TEXT PRIMARY KEY, name TEXT)")
    raw.commit()
    raw.close()

    db = Database(str(acct), market_db_path=str(market))
    db.initialize()   # _drop_shadow_tables runs here
    try:
        # shadow gone from the account DB...
        assert db.conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='stocks'"
        ).fetchone() is None
        # ...so unqualified read now resolves to market.stocks (1 row), not 0.
        assert db.conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0] == 1
    finally:
        db.close()


def test_nonempty_shadow_preserved_not_silently_deleted(tmp_path):
    market, acct = tmp_path / "market.db", tmp_path / "acct.db"
    _market_with_one_stock(market)
    raw = sqlite3.connect(str(acct))
    raw.execute("CREATE TABLE pool_stocks (pool_name TEXT, code TEXT)")
    raw.execute("INSERT INTO pool_stocks VALUES ('mine', '000002')")  # user data
    raw.commit()
    raw.close()

    db = Database(str(acct), market_db_path=str(market))
    db.initialize()
    try:
        # non-empty shadow is KEPT (loudly flagged, not auto-deleted)
        assert db.conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='pool_stocks'"
        ).fetchone() is not None
        assert db.conn.execute(
            "SELECT COUNT(*) FROM main.pool_stocks").fetchone()[0] == 1
    finally:
        db.close()


def test_single_file_mode_is_noop(tmp_path):
    # No market attached → the shared tables legitimately live in main; the
    # guard must NOT drop them.
    db = Database(str(tmp_path / "solo.db"))
    db.initialize()
    try:
        db.upsert_stock("000001", "x", "SZ", date(1991, 4, 3), "银行")
        assert db.conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0] == 1
    finally:
        db.close()
