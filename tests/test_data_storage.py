"""Tests for database storage layer."""

from datetime import date

import pytest

from quanti.data.database import Database


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    database.initialize()
    yield database
    database.close()


class TestStockStorage:
    def test_upsert_and_get_stock(self, db):
        db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
        stock = db.get_stock("000001")
        assert stock is not None
        assert stock.name == "平安银行"
        assert stock.exchange == "SZ"

    def test_get_nonexistent_stock(self, db):
        stock = db.get_stock("999999")
        assert stock is None

    def test_list_all_stocks(self, db):
        db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
        db.upsert_stock("600519", "贵州茅台", "SH", date(2001, 8, 27), "白酒")
        stocks = db.list_stocks()
        assert len(stocks) == 2


class TestDailyQuoteStorage:
    def test_save_and_get_daily_quotes(self, db):
        import pandas as pd

        df = pd.DataFrame(
            {
                "code": ["000001", "000001"],
                "date": [date(2024, 1, 2), date(2024, 1, 3)],
                "open": [10.0, 10.2],
                "high": [10.5, 10.6],
                "low": [9.8, 10.0],
                "close": [10.2, 10.5],
                "volume": [1_000_000, 1_200_000],
                "amount": [10_200_000, 12_600_000],
                "turnover": [1.5, 1.8],
            }
        )
        db.save_daily_quotes(df)
        result = db.get_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 5))
        assert len(result) == 2
        assert result.iloc[0]["close"] == 10.2
        # No adj_factor supplied → defaults to 1.0 (backward compatible).
        assert "adj_factor" in result.columns
        assert list(result["adj_factor"]) == [1.0, 1.0]

    def test_adj_factor_round_trip_and_clamp(self, db):
        import math

        import pandas as pd
        df = pd.DataFrame({
            "code": ["000001"] * 4,
            "date": [date(2024, 1, i) for i in (2, 3, 4, 5)],
            "open": [10.0] * 4, "high": [10.5] * 4, "low": [9.8] * 4,
            "close": [10.0] * 4, "volume": [1e6] * 4, "amount": [1e7] * 4,
            "turnover": [1.0] * 4,
            # valid, then three poisoned values that must clamp to 1.0.
            "adj_factor": [1.25, 0.0, -3.0, math.nan],
        })
        db.save_daily_quotes(df)
        result = db.get_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 9))
        assert list(result["adj_factor"]) == [1.25, 1.0, 1.0, 1.0]
        # raw prices are stored untouched regardless of factor.
        assert list(result["close"]) == [10.0, 10.0, 10.0, 10.0]

    def test_get_latest_date(self, db):
        import pandas as pd

        df = pd.DataFrame(
            {
                "code": ["000001"],
                "date": [date(2024, 1, 2)],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1_000_000],
                "amount": [10_200_000],
                "turnover": [1.5],
            }
        )
        db.save_daily_quotes(df)
        latest = db.get_latest_quote_date("000001")
        assert latest == date(2024, 1, 2)

    def test_get_latest_date_no_data(self, db):
        latest = db.get_latest_quote_date("000001")
        assert latest is None

    def test_adj_factor_migration_on_legacy_db(self, tmp_path):
        """A pre-existing daily_quotes WITHOUT adj_factor gains the column (=1.0)
        via _migrate, without losing data — and is idempotent on re-init."""
        import sqlite3

        path = str(tmp_path / "legacy.db")
        con = sqlite3.connect(path)
        con.execute("""
            CREATE TABLE daily_quotes (
                code TEXT NOT NULL, date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume REAL, amount REAL, turnover REAL DEFAULT 0,
                PRIMARY KEY (code, date))""")
        con.execute("INSERT INTO daily_quotes VALUES "
                    "('000001','2024-01-02',10,10.5,9.8,10.2,1e6,1e7,1.5)")
        con.commit()
        con.close()

        d = Database(path)
        d.initialize()  # runs _migrate → ADD COLUMN adj_factor DEFAULT 1.0
        cols = [r[1] for r in d.conn.execute(
            "PRAGMA table_info(daily_quotes)").fetchall()]
        assert "adj_factor" in cols
        res = d.get_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 9))
        assert len(res) == 1 and res.iloc[0]["close"] == 10.2  # data intact
        assert res.iloc[0]["adj_factor"] == 1.0                # backfilled
        d._migrate()  # idempotent — no error, no duplicate column
        d.close()


class TestTradeCalendar:
    def test_save_and_get_trade_dates(self, db):
        dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
        db.save_trade_calendar(dates)
        result = db.get_trade_dates(date(2024, 1, 1), date(2024, 1, 5))
        assert len(result) == 3

    def test_is_trade_date(self, db):
        dates = [date(2024, 1, 2), date(2024, 1, 3)]
        db.save_trade_calendar(dates)
        assert db.is_trade_date(date(2024, 1, 2)) is True
        assert db.is_trade_date(date(2024, 1, 1)) is False


class TestAccountMarketSplit:
    """Split mode: trading state in the account DB, market data in a shared
    market DB. Keeps real money (live.db) isolated from paper, market synced
    once. Unqualified queries resolve across the attach (no query changes)."""

    def test_tables_land_in_right_db(self, tmp_path):
        db = Database(str(tmp_path / "paper.db"),
                      market_db_path=str(tmp_path / "market.db"))
        db.initialize()
        main = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        mkt = {r[0] for r in db.conn.execute(
            "SELECT name FROM market.sqlite_master WHERE type='table'").fetchall()}
        assert {"positions", "orders", "portfolio_state"} <= main
        assert {"daily_quotes", "stocks", "news_sentiment"} <= mkt
        assert "daily_quotes" not in main and "positions" not in mkt
        db.close()

    def test_writes_route_across_attach(self, tmp_path):
        import pandas as pd
        db = Database(str(tmp_path / "paper.db"),
                      market_db_path=str(tmp_path / "market.db"))
        db.initialize()
        # Market write → market DB; trading write → account DB. Both via
        # unqualified names (the broker/agent code is unchanged).
        db.save_daily_quotes(pd.DataFrame({
            "code": ["X"], "date": ["2026-06-16"], "open": [1.0], "high": [1.0],
            "low": [1.0], "close": [1.0], "volume": [1.0], "amount": [1.0],
            "turnover": [1.0]}))
        db.upsert_position("X", 100, 1.0, 1.0, date(2026, 6, 16))
        assert db.conn.execute(
            "SELECT COUNT(*) FROM market.daily_quotes").fetchone()[0] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM main.positions").fetchone()[0] == 1
        # And the high-level readers work unchanged.
        assert db.get_latest_quote_date("X") == date(2026, 6, 16)
        assert len(db.list_positions()) == 1
        db.close()

    def test_two_accounts_share_market_isolate_trades(self, tmp_path):
        """paper + live attach the same market DB but keep separate trades."""
        mkt = str(tmp_path / "market.db")
        paper = Database(str(tmp_path / "paper.db"), market_db_path=mkt)
        paper.initialize()
        live = Database(str(tmp_path / "live.db"), market_db_path=mkt)
        live.initialize()
        paper.upsert_position("AAA", 100, 10.0, 10.0, date(2026, 6, 16))
        # Live sees no paper position; trades are isolated by file.
        assert len(paper.list_positions()) == 1
        assert len(live.list_positions()) == 0
        paper.close()
        live.close()
