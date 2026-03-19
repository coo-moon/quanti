"""SQLite database layer for market data storage."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from quanti.models import StockInfo


class Database:
    """SQLite-based storage for market data."""

    def __init__(self, db_path: str = "data/quanti.db"):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Create database and tables."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    def _create_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stocks (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                list_date TEXT NOT NULL,
                industry TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS daily_quotes (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                amount REAL NOT NULL,
                turnover REAL DEFAULT 0,
                PRIMARY KEY (code, date)
            );

            CREATE TABLE IF NOT EXISTS trade_calendar (
                date TEXT PRIMARY KEY
            );

            CREATE INDEX IF NOT EXISTS idx_daily_quotes_code
                ON daily_quotes(code);
            CREATE INDEX IF NOT EXISTS idx_daily_quotes_date
                ON daily_quotes(date);
            """
        )

    # --- Stock operations ---

    def upsert_stock(
        self,
        code: str,
        name: str,
        exchange: str,
        list_date: date,
        industry: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO stocks (code, name, exchange, list_date, industry)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                exchange=excluded.exchange,
                list_date=excluded.list_date,
                industry=excluded.industry
            """,
            (code, name, exchange, list_date.isoformat(), industry),
        )
        self.conn.commit()

    def get_stock(self, code: str) -> StockInfo | None:
        row = self.conn.execute(
            "SELECT code, name, exchange, list_date, industry FROM stocks WHERE code=?",
            (code,),
        ).fetchone()
        if row is None:
            return None
        return StockInfo(
            code=row[0],
            name=row[1],
            exchange=row[2],
            list_date=date.fromisoformat(row[3]),
            industry=row[4],
        )

    def list_stocks(self) -> list[StockInfo]:
        rows = self.conn.execute(
            "SELECT code, name, exchange, list_date, industry FROM stocks ORDER BY code"
        ).fetchall()
        return [
            StockInfo(
                code=r[0],
                name=r[1],
                exchange=r[2],
                list_date=date.fromisoformat(r[3]),
                industry=r[4],
            )
            for r in rows
        ]

    # --- Daily quote operations ---

    def save_daily_quotes(self, df: pd.DataFrame) -> int:
        """Save daily quotes from DataFrame. Returns number of rows inserted."""
        records = []
        for _, row in df.iterrows():
            d = row["date"]
            date_str = d.isoformat() if isinstance(d, date) else str(d)
            records.append(
                (
                    row["code"],
                    date_str,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                    float(row["amount"]),
                    float(row.get("turnover", 0)),
                )
            )
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO daily_quotes
                (code, date, open, high, low, close, volume, amount, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
        self.conn.commit()
        return len(records)

    def get_daily_quotes(
        self, code: str, start: date, end: date
    ) -> pd.DataFrame:
        """Get daily quotes for a stock within date range."""
        df = pd.read_sql_query(
            """
            SELECT code, date, open, high, low, close, volume, amount, turnover
            FROM daily_quotes
            WHERE code=? AND date>=? AND date<=?
            ORDER BY date
            """,
            self.conn,
            params=(code, start.isoformat(), end.isoformat()),
        )
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def get_latest_quote_date(self, code: str) -> date | None:
        row = self.conn.execute(
            "SELECT MAX(date) FROM daily_quotes WHERE code=?", (code,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return date.fromisoformat(row[0])

    # --- Trade calendar ---

    def save_trade_calendar(self, dates: list[date]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO trade_calendar (date) VALUES (?)",
            [(d.isoformat(),) for d in dates],
        )
        self.conn.commit()

    def get_trade_dates(self, start: date, end: date) -> list[date]:
        rows = self.conn.execute(
            "SELECT date FROM trade_calendar WHERE date>=? AND date<=? ORDER BY date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [date.fromisoformat(r[0]) for r in rows]

    def is_trade_date(self, d: date) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM trade_calendar WHERE date=?", (d.isoformat(),)
        ).fetchone()
        return row is not None
