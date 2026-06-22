"""SQLite database layer for market data storage."""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quanti.models import StockInfo


import threading

logger = logging.getLogger(__name__)


def _nan_to_none(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


class _Result:
    """Materialized result of one ``execute()``.

    The rows are fetched while the connection lock is still held, so a query is
    atomic end-to-end. Returning a live sqlite cursor and letting the caller
    ``.fetchone()/.fetchall()`` *after* the lock was released let a concurrent
    ``execute`` on the shared connection corrupt the read (torn rows with NULL
    columns) — see :class:`_LockedConnection`.
    """

    def __init__(self, rows, rowcount, lastrowid, description) -> None:
        self._rows = list(rows)
        self._i = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = description

    def fetchone(self):
        if self._i < len(self._rows):
            row = self._rows[self._i]
            self._i += 1
            return row
        return None

    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def fetchmany(self, size: int = 1):
        out = self._rows[self._i:self._i + size]
        self._i += len(out)
        return out

    def __iter__(self):
        start = self._i
        self._i = len(self._rows)
        return iter(self._rows[start:])


class _LockedConnection:
    """sqlite3.Connection wrapper that serializes all execute/commit/etc
    operations behind a shared lock.

    Reason for existing: the original Database held a single sqlite3.Connection
    with check_same_thread=False but no synchronization. As of 2026-05-28
    we have multiple writer threads (AgentRuntime, BackgroundQuoteSyncer,
    user-triggered API syncs) hitting the same connection concurrently,
    which produces SQLite "bad parameter or other API misuse" errors
    because the connection's internal cursor/transaction state corrupts.

    The fix is to serialize everything through one re-entrant lock.
    SQLite is fundamentally single-writer anyway — serializing in Python
    just stops us from corrupting the API.

    Why a thin wrapper instead of `with db._lock` everywhere: the codebase
    has ~80 call sites of `self.conn.execute(...)` / `pd.read_sql_query(...,
    self.conn)`. Wrapping the connection itself touches one file.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, *args, **kwargs):
        # Materialize the result UNDER the lock. The connection is shared across
        # threads (AgentRuntime loop + web requests + BackgroundQuoteSyncer);
        # returning a live cursor and letting the caller fetch *outside* the
        # lock lets a concurrent execute on the same connection corrupt the read
        # (e.g. a torn row surfacing NULLs). Fetching here keeps each query
        # atomic. See _Result.
        with self._lock:
            cur = self._conn.execute(*args, **kwargs)
            rows = cur.fetchall() if cur.description is not None else []
            return _Result(rows, cur.rowcount, cur.lastrowid, cur.description)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return self._conn.executemany(*args, **kwargs)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def rollback(self):
        with self._lock:
            return self._conn.rollback()

    def cursor(self):
        # Cursors are caller-managed; the lock should be held by the caller
        # while iterating. In this codebase cursors are only used briefly
        # and we don't have a documented pattern for them, so this is
        # best-effort — wrap individual cursor operations.
        with self._lock:
            return self._conn.cursor()

    def close(self):
        with self._lock:
            return self._conn.close()

    # pandas.read_sql_query passes the connection through DBAPI cursor
    # introspection. Expose the lock-acquiring proxy attributes it needs.
    def __getattr__(self, name):
        # Anything we didn't wrap (e.g. row_factory, in_transaction) falls
        # through. These don't mutate the cursor state so are safe to
        # access without the lock; pandas only reads them.
        return getattr(self._conn, name)


class Database:
    """SQLite-based storage for market data."""

    def __init__(self, db_path: str = "data/quanti.db",
                 market_db_path: str | None = None):
        """Args:
            db_path: the account DB — holds trading state (portfolio, positions,
                orders, trades, goal, decisions). Each account (paper / live)
                gets its own file, so real money is never mixed with paper.
            market_db_path: shared market DB (stocks, daily_quotes, calendar,
                pools, sync_jobs, news_sentiment), ATTACHed as `market`. When
                None, everything lives in db_path as before (single-file mode,
                used by tests). Market tables sync once and both accounts read
                them; SQLite resolves unqualified names across the attach, so
                queries don't change.
        """
        self._db_path = db_path
        self._market_db_path = market_db_path
        self._raw_conn: sqlite3.Connection | None = None
        self._conn: _LockedConnection | None = None
        self._db_lock = threading.RLock()

    def initialize(self) -> None:
        """Create database and tables."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._raw_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._raw_conn.execute("PRAGMA journal_mode=WAL")
        if self._market_db_path:
            Path(self._market_db_path).parent.mkdir(parents=True, exist_ok=True)
            self._raw_conn.execute("ATTACH DATABASE ? AS market",
                                   (self._market_db_path,))
            self._raw_conn.execute("PRAGMA market.journal_mode=WAL")
        self._conn = _LockedConnection(self._raw_conn, self._db_lock)
        self._create_tables()
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent additive migrations for DBs created before a column
        existed. SQLite ADD COLUMN is cheap and non-locking."""
        adds = [
            ("positions", "entry_strategy", "TEXT DEFAULT ''"),
            ("orders", "entry_strategy", "TEXT DEFAULT ''"),
            ("stocks", "delist_date", "TEXT"),
            # Qlib-style price adjustment: store RAW prices + a per-(code,date)
            # back-adjustment factor. Pre-existing rows backfill to 1.0 (read as
            # raw) until a `quanti sync --refetch` rewrites them as raw+factor.
            ("daily_quotes", "adj_factor", "REAL DEFAULT 1.0"),
            # T+1: shares bought today are frozen (unsellable) until tomorrow.
            # frozen_qty applies only on frozen_date (today's buys); SELLs are
            # capped at quantity - frozen so a same-day add-on can't be sold.
            ("positions", "frozen_qty", "REAL DEFAULT 0"),
            ("positions", "frozen_date", "TEXT"),
        ]
        for table, col, decl in adds:
            cols = [r[1] for r in self.conn.execute(
                f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
            self._raw_conn = None

    @property
    def conn(self):
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    def _create_tables(self) -> None:
        # Market / shared data → `market.` schema when a market DB is attached,
        # otherwise the main DB (single-file mode). SQLite resolves unqualified
        # names across the attach, so only DDL needs the prefix; queries don't.
        m = "market." if self._market_db_path else ""
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {m}stocks (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                list_date TEXT NOT NULL,
                industry TEXT DEFAULT '',
                delist_date TEXT
            );

            CREATE TABLE IF NOT EXISTS {m}daily_quotes (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                amount REAL NOT NULL,
                turnover REAL DEFAULT 0,
                adj_factor REAL NOT NULL DEFAULT 1.0,
                PRIMARY KEY (code, date)
            );

            CREATE TABLE IF NOT EXISTS {m}trade_calendar (
                date TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS {m}stock_pools (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                description TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS {m}pool_stocks (
                pool_name TEXT NOT NULL,
                code TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (pool_name, code)
            );

            CREATE TABLE IF NOT EXISTS {m}sync_jobs (
                job_id TEXT PRIMARY KEY,
                pool_name TEXT NOT NULL,
                total INTEGER NOT NULL,
                current INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                errors_json TEXT DEFAULT '{{}}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS {m}news_sentiment (
                code TEXT NOT NULL,
                as_of TEXT NOT NULL,
                score REAL NOT NULL,
                reason TEXT DEFAULT '',
                n_news INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (code, as_of)
            );

            CREATE INDEX IF NOT EXISTS {m}idx_daily_quotes_code
                ON daily_quotes(code);
            CREATE INDEX IF NOT EXISTS {m}idx_daily_quotes_date
                ON daily_quotes(date);
            """
        )
        # Trading state → always the main (account) DB.
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS portfolio_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL,
                initial_cash REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                code TEXT PRIMARY KEY,
                quantity INTEGER NOT NULL,
                avg_cost REAL NOT NULL,
                current_price REAL DEFAULT 0,
                buy_date TEXT,
                updated_at TEXT NOT NULL,
                entry_strategy TEXT DEFAULT '',
                frozen_qty REAL NOT NULL DEFAULT 0,
                frozen_date TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                direction TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price_type TEXT NOT NULL,
                limit_price REAL DEFAULT 0,
                status TEXT NOT NULL,
                strategy_name TEXT DEFAULT '',
                filled_price REAL DEFAULT 0,
                filled_quantity INTEGER DEFAULT 0,
                reason TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                filled_at TEXT,
                entry_strategy TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                order_id TEXT,
                code TEXT NOT NULL,
                direction TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                commission REAL NOT NULL,
                strategy_name TEXT DEFAULT '',
                trade_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                snapshot_date TEXT PRIMARY KEY,
                cash REAL NOT NULL,
                market_value REAL NOT NULL,
                total_value REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            -- Agent / goal -----------------------------------------------

            CREATE TABLE IF NOT EXISTS agent_goal (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                target_annual_return REAL NOT NULL,
                max_drawdown REAL NOT NULL,
                risk_tolerance TEXT NOT NULL,
                universe_pool TEXT DEFAULT '',
                screener_name TEXT DEFAULT '',
                strategy_name TEXT DEFAULT '',
                params_json TEXT DEFAULT '{}',
                rebalance_freq TEXT NOT NULL DEFAULT 'daily',
                enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                code TEXT DEFAULT '',
                summary TEXT NOT NULL,
                details_json TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_trades_date
                ON trades(trade_date);
            CREATE INDEX IF NOT EXISTS idx_orders_created
                ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_decisions_ts
                ON agent_decisions(ts);

            CREATE TABLE IF NOT EXISTS strategy_params (
                strategy_name TEXT PRIMARY KEY,
                params_json TEXT NOT NULL,
                oos_sharpe REAL,
                baseline_oos_sharpe REAL,
                accepted INTEGER NOT NULL,
                n_combos INTEGER,
                universe_size INTEGER,
                tuned_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generated_factors (
                name TEXT PRIMARY KEY,
                expr_str TEXT NOT NULL,
                train_ic REAL,
                oos_ic REAL,
                accepted INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
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
        delist_date: date | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO stocks (code, name, exchange, list_date, industry, delist_date)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                exchange=excluded.exchange,
                list_date=excluded.list_date,
                industry=excluded.industry,
                delist_date=COALESCE(excluded.delist_date, stocks.delist_date)
            """,
            (
                code,
                name,
                exchange,
                list_date.isoformat(),
                industry,
                delist_date.isoformat() if delist_date else None,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _safe_list_date(v) -> date:
        """Parse a stored list_date defensively.

        A malformed/missing value must NEVER crash universe building — one bad
        stock row (seen in the wild for quirky new-board names whose feed
        returns a non-str/garbage list date) would otherwise kill the entire
        agent tick via `_filter_metadata → get_stock`. Falls back to a very old
        date so the stock is treated as long-listed rather than dropped;
        downstream liquidity / ST / risk filters still apply.
        """
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v)[:10])
        except (ValueError, TypeError):
            return date(1990, 1, 1)

    @staticmethod
    def _safe_delist_date(v) -> date | None:
        """Parse a stored delist_date. NULL / empty / garbage → None (treated as
        still listed). Mirrors _safe_list_date's defensiveness but defaults to
        None rather than an epoch date."""
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in ("none", "nan"):
            return None
        try:
            return date.fromisoformat(s[:10])
        except (ValueError, TypeError):
            return None

    def get_stock(self, code: str) -> StockInfo | None:
        row = self.conn.execute(
            "SELECT code, name, exchange, list_date, industry, delist_date "
            "FROM stocks WHERE code=?",
            (code,),
        ).fetchone()
        if row is None:
            return None
        return StockInfo(
            code=row[0],
            name=row[1],
            exchange=row[2],
            list_date=self._safe_list_date(row[3]),
            industry=row[4],
            delist_date=self._safe_delist_date(row[5]),
        )

    def list_stocks(self) -> list[StockInfo]:
        rows = self.conn.execute(
            "SELECT code, name, exchange, list_date, industry, delist_date "
            "FROM stocks ORDER BY code"
        ).fetchall()
        return [
            StockInfo(
                code=r[0],
                name=r[1],
                exchange=r[2],
                list_date=self._safe_list_date(r[3]),
                industry=r[4],
                delist_date=self._safe_delist_date(r[5]),
            )
            for r in rows
        ]

    def point_in_time_universe(self, start: date, end: date) -> list[str]:
        """Codes that were alive at some point within [start, end] — the
        survivorship-bias-free backtest universe.

        A stock qualifies if it had listed on/before the window end AND had not
        yet delisted at the window start (delist_date NULL = still listed).
        list_date/delist_date are stored as ISO YYYY-MM-DD, so lexicographic
        string comparison equals date comparison.
        """
        rows = self.conn.execute(
            """
            SELECT code FROM stocks
            WHERE list_date <= ?
              AND (delist_date IS NULL OR delist_date >= ?)
            ORDER BY code
            """,
            (end.isoformat(), start.isoformat()),
        ).fetchall()
        return [r[0] for r in rows]

    # --- Daily quote operations ---

    def save_daily_quotes(self, df: pd.DataFrame) -> int:
        """Save daily quotes from DataFrame. Returns number of rows inserted."""
        records = []
        for _, row in df.iterrows():
            d = row["date"]
            date_str = d.isoformat() if isinstance(d, date) else str(d)
            # Back-adjustment factor: raw_price × adj_factor = hfq price. Default
            # 1.0 (raw) and clamp NaN/inf/≤0 to 1.0 — a bad factor would silently
            # zero/negate prices for every reader.
            f = float(row.get("adj_factor", 1.0) or 1.0)
            if not math.isfinite(f) or f <= 0:
                f = 1.0
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
                    f,
                )
            )
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO daily_quotes
                (code, date, open, high, low, close, volume, amount, turnover,
                 adj_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
        self.conn.commit()
        return len(records)

    def get_daily_quotes(
        self, code: str, start: date, end: date
    ) -> pd.DataFrame:
        """Get daily quotes for a stock within date range.

        pandas.read_sql_query iterates a cursor internally — our
        _LockedConnection wrapper only locks each method call, not the
        whole multi-step iteration. So we hold the DB lock manually
        around the read to prevent it racing with the BackgroundQuoteSyncer
        writer thread. SQLite's "bad parameter or other API misuse" was
        the symptom of that race.
        """
        with self._db_lock:
            df = pd.read_sql_query(
                """
                SELECT code, date, open, high, low, close, volume, amount,
                       turnover, adj_factor
                FROM daily_quotes
                WHERE code=? AND date>=? AND date<=?
                ORDER BY date
                """,
                self._raw_conn,
                params=(code, start.isoformat(), end.isoformat()),
            )
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def get_adv20_map(self, start: date, end: date,
                      window: int = 20) -> dict[str, float]:
        """Mean turnover (`amount`) over each code's most-recent `window` bars
        within [start, end], for every code that traded in the window — in a
        SINGLE query.

        `sort_by_adv20` used to read bars per code: ranking the whole ~5500-name
        universe took ~22s of N round-trips. This batches it into one windowed
        aggregate (codes absent from the window simply don't appear → the caller
        treats them as ADV 0). Semantics match the old `bars[-20:]` mean: the
        most recent `window` bars by date, or all of them if fewer exist.
        """
        with self._db_lock:
            rows = self._raw_conn.execute(
                """
                SELECT code, AVG(amount) AS adv FROM (
                    SELECT code, amount,
                           ROW_NUMBER() OVER (
                               PARTITION BY code ORDER BY date DESC) AS rn
                    FROM daily_quotes
                    WHERE date >= ? AND date <= ?
                )
                WHERE rn <= ?
                GROUP BY code
                """,
                (start.isoformat(), end.isoformat(), window),
            ).fetchall()
        return {r[0]: float(r[1] or 0.0) for r in rows}

    @staticmethod
    def _safe_quote_date(v) -> date | None:
        """Tolerant parse of a stored quote date. Garbage → None ("no usable
        data"), so the next sync cold-starts and overwrites the bad value."""
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v)[:10])
        except (ValueError, TypeError):
            return None

    def get_latest_quote_date(self, code: str) -> date | None:
        row = self.conn.execute(
            "SELECT MAX(date) FROM daily_quotes WHERE code=?", (code,)
        ).fetchone()
        return self._safe_quote_date(row[0]) if row else None

    def get_global_latest_quote_date(self) -> date | None:
        """Newest bar date across the whole daily_quotes table (any code)."""
        row = self.conn.execute("SELECT MAX(date) FROM daily_quotes").fetchone()
        return self._safe_quote_date(row[0]) if row else None

    def get_high_water(self, code: str, since: date) -> float | None:
        """Highest intraday high for `code` on/after `since` (the post-entry
        peak, for trailing take-profit). None if no bars in range."""
        row = self.conn.execute(
            "SELECT MAX(high) FROM daily_quotes WHERE code=? AND date>=?",
            (code, since.isoformat()),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return float(row[0])
        except (ValueError, TypeError):
            return None

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

    # --- Stock pool operations ---

    def create_pool(self, name: str, description: str = "") -> None:
        from datetime import datetime
        self.conn.execute(
            "INSERT INTO stock_pools (name, created_at, description) VALUES (?, ?, ?)",
            (name, datetime.now().isoformat(), description),
        )
        self.conn.commit()

    def delete_pool(self, name: str) -> bool:
        cursor = self.conn.execute("DELETE FROM stock_pools WHERE name=?", (name,))
        self.conn.execute("DELETE FROM pool_stocks WHERE pool_name=?", (name,))
        self.conn.commit()
        return cursor.rowcount > 0

    def list_pools(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT p.name, p.created_at, p.description,
                   COUNT(ps.code) as stock_count
            FROM stock_pools p
            LEFT JOIN pool_stocks ps ON p.name = ps.pool_name
            GROUP BY p.name
            ORDER BY p.created_at DESC
            """
        ).fetchall()
        return [
            {"name": r[0], "created_at": r[1], "description": r[2], "stock_count": r[3]}
            for r in rows
        ]

    def pool_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM stock_pools WHERE name=?", (name,)
        ).fetchone()
        return row is not None

    def add_stocks_to_pool(self, pool_name: str, codes: list[str]) -> int:
        from datetime import datetime
        now = datetime.now().isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO pool_stocks (pool_name, code, added_at) VALUES (?, ?, ?)",
            [(pool_name, code, now) for code in codes],
        )
        self.conn.commit()
        return len(codes)

    def remove_stocks_from_pool(self, pool_name: str, codes: list[str]) -> int:
        self.conn.executemany(
            "DELETE FROM pool_stocks WHERE pool_name=? AND code=?",
            [(pool_name, code) for code in codes],
        )
        self.conn.commit()
        return len(codes)

    def get_pool_stocks(self, pool_name: str) -> list[StockInfo]:
        rows = self.conn.execute(
            """
            SELECT s.code, s.name, s.exchange, s.list_date, s.industry, s.delist_date
            FROM pool_stocks ps
            JOIN stocks s ON ps.code = s.code
            WHERE ps.pool_name = ?
            ORDER BY ps.added_at DESC
            """,
            (pool_name,),
        ).fetchall()
        return [
            StockInfo(code=r[0], name=r[1], exchange=r[2], list_date=self._safe_list_date(r[3]), industry=r[4], delist_date=self._safe_delist_date(r[5]))
            for r in rows
        ]

    def get_pool_codes(self, pool_name: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT code FROM pool_stocks WHERE pool_name=?",
            (pool_name,),
        ).fetchall()
        return [r[0] for r in rows]

    # --- Sync job tracking ---

    def create_sync_job(self, job_id: str, pool_name: str, total: int) -> None:
        from datetime import datetime
        self.conn.execute(
            "INSERT INTO sync_jobs (job_id, pool_name, total, current, status, errors_json, created_at) VALUES (?, ?, ?, 0, 'running', '{}', ?)",
            (job_id, pool_name, total, datetime.now().isoformat()),
        )
        self.conn.commit()

    def update_sync_job(self, job_id: str, current: int, status: str, errors: dict) -> None:
        import json
        self.conn.execute(
            "UPDATE sync_jobs SET current=?, status=?, errors_json=? WHERE job_id=?",
            (current, status, json.dumps(errors), job_id),
        )
        self.conn.commit()

    def get_sync_job(self, job_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT job_id, pool_name, total, current, status, errors_json, created_at FROM sync_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        import json
        return {
            "job_id": row[0], "pool_name": row[1], "total": row[2],
            "current": row[3], "status": row[4],
            "errors": json.loads(row[5]), "created_at": row[6],
        }

    # --- Strategy hyperopt params ---

    def save_optimization(self, strategy_name: str, params: dict,
                          oos_sharpe: float, baseline_oos_sharpe: float,
                          accepted: bool, n_combos: int,
                          universe_size: int) -> None:
        import json
        from datetime import datetime
        self.conn.execute(
            "INSERT OR REPLACE INTO strategy_params "
            "(strategy_name, params_json, oos_sharpe, baseline_oos_sharpe, "
            " accepted, n_combos, universe_size, tuned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (strategy_name, json.dumps(params), float(oos_sharpe),
             float(baseline_oos_sharpe), 1 if accepted else 0, int(n_combos),
             int(universe_size), datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_active_params(self, strategy_name: str) -> dict | None:
        import json
        row = self.conn.execute(
            "SELECT params_json, accepted FROM strategy_params WHERE strategy_name=?",
            (strategy_name,),
        ).fetchone()
        if row is None or not row[1]:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None

    def list_optimization_results(self) -> list[dict]:
        import json
        rows = self.conn.execute(
            "SELECT strategy_name, params_json, oos_sharpe, baseline_oos_sharpe, "
            "accepted, n_combos, universe_size, tuned_at FROM strategy_params "
            "ORDER BY strategy_name",
        ).fetchall()
        out = []
        for r in rows:
            try:
                params = json.loads(r[1])
            except (ValueError, TypeError):
                params = {}
            out.append({"strategy_name": r[0], "params": params,
                        "oos_sharpe": r[2], "baseline_oos_sharpe": r[3],
                        "accepted": bool(r[4]), "n_combos": r[5],
                        "universe_size": r[6], "tuned_at": r[7]})
        return out

    # --- Portfolio / positions / orders / trades ---

    def ensure_portfolio(self, initial_cash: float) -> dict:
        """Create the singleton portfolio row if missing, return its current state."""
        from datetime import datetime
        now = datetime.now().isoformat()
        row = self.conn.execute(
            "SELECT cash, initial_cash, created_at, updated_at FROM portfolio_state WHERE id=1"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO portfolio_state (id, cash, initial_cash, created_at, updated_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (initial_cash, initial_cash, now, now),
            )
            self.conn.commit()
            return {"cash": initial_cash, "initial_cash": initial_cash,
                    "created_at": now, "updated_at": now}
        return {"cash": row[0], "initial_cash": row[1],
                "created_at": row[2], "updated_at": row[3]}

    def reset_portfolio(self, initial_cash: float) -> None:
        from datetime import datetime
        now = datetime.now().isoformat()
        self.conn.executescript(
            "DELETE FROM positions; DELETE FROM orders; DELETE FROM trades; "
            "DELETE FROM portfolio_snapshots; DELETE FROM portfolio_state;"
        )
        self.conn.execute(
            "INSERT INTO portfolio_state (id, cash, initial_cash, created_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (initial_cash, initial_cash, now, now),
        )
        self.conn.commit()

    def update_cash(self, cash: float) -> None:
        from datetime import datetime
        self.conn.execute(
            "UPDATE portfolio_state SET cash=?, updated_at=? WHERE id=1",
            (cash, datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_portfolio_state(self) -> dict | None:
        row = self.conn.execute(
            "SELECT cash, initial_cash, created_at, updated_at FROM portfolio_state WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        return {"cash": row[0], "initial_cash": row[1],
                "created_at": row[2], "updated_at": row[3]}

    def upsert_position(self, code: str, quantity: int, avg_cost: float,
                        current_price: float, buy_date: date | None,
                        entry_strategy: str | None = None,
                        frozen_qty: float = 0,
                        frozen_date: date | None = None) -> None:
        from datetime import datetime
        # entry_strategy is only set on the FIRST buy (position open). On a
        # follow-on buy (averaging in) we pass None to preserve the original
        # owning strategy via COALESCE rather than overwriting it.
        # frozen_qty/frozen_date track today's T+1-frozen lot (callers compute
        # them); they overwrite on every upsert (the caller owns the new value).
        self.conn.execute(
            """
            INSERT INTO positions (code, quantity, avg_cost, current_price,
                                   buy_date, updated_at, entry_strategy,
                                   frozen_qty, frozen_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                quantity=excluded.quantity,
                avg_cost=excluded.avg_cost,
                current_price=excluded.current_price,
                buy_date=excluded.buy_date,
                updated_at=excluded.updated_at,
                entry_strategy=COALESCE(
                    NULLIF(excluded.entry_strategy, ''), positions.entry_strategy),
                frozen_qty=excluded.frozen_qty,
                frozen_date=excluded.frozen_date
            """,
            (code, quantity, avg_cost, current_price,
             buy_date.isoformat() if buy_date else None,
             datetime.now().isoformat(), entry_strategy or "",
             float(frozen_qty or 0),
             frozen_date.isoformat() if frozen_date else None),
        )
        self.conn.commit()

    def delete_position(self, code: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE code=?", (code,))
        self.conn.commit()

    def list_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT code, quantity, avg_cost, current_price, buy_date, "
            "updated_at, entry_strategy, frozen_qty, frozen_date FROM positions"
        ).fetchall()
        return [
            {
                "code": r[0], "quantity": r[1], "avg_cost": r[2],
                "current_price": r[3],
                "buy_date": date.fromisoformat(r[4]) if r[4] else None,
                "updated_at": r[5],
                "entry_strategy": r[6] or "",
                "frozen_qty": r[7] or 0,
                "frozen_date": date.fromisoformat(r[8]) if r[8] else None,
            }
            for r in rows
        ]

    def set_position_price(self, code: str, price: float) -> None:
        from datetime import datetime
        self.conn.execute(
            "UPDATE positions SET current_price=?, updated_at=? WHERE code=?",
            (price, datetime.now().isoformat(), code),
        )
        self.conn.commit()

    def insert_order(self, order: dict) -> None:
        from datetime import datetime
        self.conn.execute(
            """
            INSERT INTO orders (order_id, code, direction, quantity, price_type, limit_price,
                                status, strategy_name, filled_price, filled_quantity,
                                reason, created_at, filled_at, entry_strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["order_id"], order["code"], order["direction"],
                order["quantity"], order["price_type"], order.get("limit_price", 0),
                order["status"], order.get("strategy_name", ""),
                order.get("filled_price", 0), order.get("filled_quantity", 0),
                order.get("reason", ""),
                order.get("created_at") or datetime.now().isoformat(),
                order.get("filled_at"),
                order.get("entry_strategy", ""),
            ),
        )
        self.conn.commit()

    def update_order_filled(self, order_id: str, status: str,
                            filled_price: float, filled_quantity: int) -> None:
        from datetime import datetime
        self.conn.execute(
            "UPDATE orders SET status=?, filled_price=?, filled_quantity=?, filled_at=? "
            "WHERE order_id=?",
            (status, filled_price, filled_quantity, datetime.now().isoformat(), order_id),
        )
        self.conn.commit()

    def list_orders(self, limit: int = 200,
                    status: str | None = None) -> list[dict]:
        """List orders, optionally filtered by status (e.g. "pending").

        When `status` is None (default), returns the most recent `limit`
        orders of any status — matches legacy behavior. With a status
        filter, callers (PaperBroker.try_fill_pending_orders) get a
        bounded queue to walk without scanning the full table.
        """
        if status:
            rows = self.conn.execute(
                "SELECT order_id, code, direction, quantity, status, filled_price, "
                "filled_quantity, strategy_name, reason, created_at, filled_at, "
                "entry_strategy "
                "FROM orders WHERE status=? ORDER BY created_at ASC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT order_id, code, direction, quantity, status, filled_price, "
                "filled_quantity, strategy_name, reason, created_at, filled_at, "
                "entry_strategy "
                "FROM orders ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "order_id": r[0], "code": r[1], "direction": r[2],
                "quantity": r[3], "status": r[4], "filled_price": r[5],
                "filled_quantity": r[6], "strategy_name": r[7],
                "reason": r[8], "created_at": r[9], "filled_at": r[10],
                "entry_strategy": r[11] or "",
            }
            for r in rows
        ]

    def stop_loss_exit_dates(self, since: date) -> list[date]:
        """Fill dates of stop-loss exits since `since`, for StoplossGuard.

        A stop-loss exit is a FILLED SELL order tagged strategy_name
        'risk_exit' whose reason starts with STOP_LOSS_REASON_PREFIX — the
        double match excludes take-profit / strategy exits that share the
        'risk_exit' tag. `since` is a generous calendar lower bound; the pure
        protection logic filters precisely by trading days."""
        from quanti.risk.manager import STOP_LOSS_REASON_PREFIX
        rows = self.conn.execute(
            "SELECT filled_at FROM orders "
            "WHERE direction='sell' AND status='filled' "
            "AND strategy_name='risk_exit' AND reason LIKE ? "
            "AND filled_at IS NOT NULL AND filled_at >= ?",
            (STOP_LOSS_REASON_PREFIX + "%", since.isoformat()),
        ).fetchall()
        out: list[date] = []
        for r in rows:
            try:
                out.append(datetime.fromisoformat(r[0]).date())
            except (ValueError, TypeError):
                continue
        return out

    def update_order_status(self, order_id: str, status: str,
                            reason: str | None = None) -> None:
        """Set an order's status (and optionally append a reason).

        Used by PaperBroker to transition pending → cancelled / rejected
        without going through update_order_filled (which assumes a fill).
        """
        if reason is not None:
            self.conn.execute(
                "UPDATE orders SET status=?, reason=? WHERE order_id=?",
                (status, reason, order_id),
            )
        else:
            self.conn.execute(
                "UPDATE orders SET status=? WHERE order_id=?",
                (status, order_id),
            )
        self.conn.commit()

    def insert_trade(self, trade: dict) -> None:
        from datetime import datetime
        self.conn.execute(
            """
            INSERT INTO trades (trade_id, order_id, code, direction, quantity, price,
                                commission, strategy_name, trade_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade["trade_id"], trade.get("order_id"), trade["code"],
                trade["direction"], trade["quantity"], trade["price"],
                trade["commission"], trade.get("strategy_name", ""),
                trade["trade_date"],
                trade.get("created_at") or datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    def list_trades(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT trade_id, order_id, code, direction, quantity, price, commission, "
            "strategy_name, trade_date, created_at FROM trades "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "trade_id": r[0], "order_id": r[1], "code": r[2],
                "direction": r[3], "quantity": r[4], "price": r[5],
                "commission": r[6], "strategy_name": r[7],
                "trade_date": r[8], "created_at": r[9],
            }
            for r in rows
        ]

    def get_peak_total_value(self) -> float:
        """Highest portfolio total_value ever recorded — the high-water mark for
        the portfolio drawdown circuit breaker. 0.0 when no snapshots exist."""
        row = self.conn.execute(
            "SELECT MAX(total_value) FROM portfolio_snapshots").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def save_portfolio_snapshot(self, snapshot_date: date, cash: float,
                                market_value: float, total_value: float) -> None:
        from datetime import datetime
        self.conn.execute(
            """
            INSERT OR REPLACE INTO portfolio_snapshots
                (snapshot_date, cash, market_value, total_value, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (snapshot_date.isoformat(), cash, market_value, total_value,
             datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_portfolio_snapshots(self, limit: int = 365) -> list[dict]:
        rows = self.conn.execute(
            "SELECT snapshot_date, cash, market_value, total_value FROM portfolio_snapshots "
            "ORDER BY snapshot_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"snapshot_date": r[0], "cash": r[1],
             "market_value": r[2], "total_value": r[3]}
            for r in rows
        ]

    # --- Agent goal & decisions ---

    def get_agent_goal(self) -> dict | None:
        row = self.conn.execute(
            "SELECT target_annual_return, max_drawdown, risk_tolerance, universe_pool, "
            "screener_name, strategy_name, params_json, rebalance_freq, enabled, updated_at "
            "FROM agent_goal WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        import json
        return {
            "target_annual_return": row[0], "max_drawdown": row[1],
            "risk_tolerance": row[2], "universe_pool": row[3],
            "screener_name": row[4], "strategy_name": row[5],
            "params": json.loads(row[6] or "{}"),
            "rebalance_freq": row[7], "enabled": bool(row[8]),
            "updated_at": row[9],
        }

    def upsert_agent_goal(self, goal: dict) -> None:
        import json
        from datetime import datetime
        now = datetime.now().isoformat()
        params_json = json.dumps(goal.get("params", {}))
        self.conn.execute(
            """
            INSERT INTO agent_goal (id, target_annual_return, max_drawdown, risk_tolerance,
                                    universe_pool, screener_name, strategy_name, params_json,
                                    rebalance_freq, enabled, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target_annual_return=excluded.target_annual_return,
                max_drawdown=excluded.max_drawdown,
                risk_tolerance=excluded.risk_tolerance,
                universe_pool=excluded.universe_pool,
                screener_name=excluded.screener_name,
                strategy_name=excluded.strategy_name,
                params_json=excluded.params_json,
                rebalance_freq=excluded.rebalance_freq,
                enabled=excluded.enabled,
                updated_at=excluded.updated_at
            """,
            (
                goal["target_annual_return"], goal["max_drawdown"],
                goal["risk_tolerance"], goal.get("universe_pool", ""),
                goal.get("screener_name", ""), goal.get("strategy_name", ""),
                params_json, goal.get("rebalance_freq", "daily"),
                1 if goal.get("enabled") else 0, now,
            ),
        )
        self.conn.commit()

    def log_decision(self, kind: str, summary: str,
                     code: str = "", details: dict | None = None) -> int:
        import json
        from datetime import datetime
        cur = self.conn.execute(
            "INSERT INTO agent_decisions (ts, kind, code, summary, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), kind, code, summary,
             json.dumps(details or {})),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def prune_decisions(self, older_than_days: int = 90) -> int:
        """Delete decision log entries older than the given number of days.

        Returns the number of rows removed. Idempotent — safe to call on every
        agent tick. The default of 90 days keeps roughly 3 months of audit
        trail at typical 4-hour cadence (~540 cycles).
        """
        if older_than_days < 1:
            return 0
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        cur = self.conn.execute(
            "DELETE FROM agent_decisions WHERE ts < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount or 0

    def list_decisions(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        import json
        if kind:
            rows = self.conn.execute(
                "SELECT id, ts, kind, code, summary, details_json FROM agent_decisions "
                "WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, ts, kind, code, summary, details_json FROM agent_decisions "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": r[0], "ts": r[1], "kind": r[2], "code": r[3],
             "summary": r[4], "details": json.loads(r[5] or "{}")}
            for r in rows
        ]

    # ----- news sentiment cache -------------------------------------
    def get_news_sentiment(self, code: str, as_of: str) -> dict | None:
        """Return the cached sentiment row for (code, as_of), or None."""
        row = self.conn.execute(
            "SELECT code, as_of, score, reason, n_news, model "
            "FROM news_sentiment WHERE code=? AND as_of=?",
            (code, as_of),
        ).fetchone()
        if row is None:
            return None
        return {"code": row[0], "as_of": row[1], "score": row[2],
                "reason": row[3], "n_news": row[4], "model": row[5]}

    def upsert_news_sentiment(self, code: str, as_of: str, score: float,
                              reason: str = "", n_news: int = 0,
                              model: str = "") -> None:
        """Insert or update one (code, as_of) sentiment score."""
        from datetime import datetime
        self.conn.execute(
            "INSERT INTO news_sentiment "
            "(code, as_of, score, reason, n_news, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(code, as_of) DO UPDATE SET "
            "score=excluded.score, reason=excluded.reason, "
            "n_news=excluded.n_news, model=excluded.model, "
            "created_at=excluded.created_at",
            (code, as_of, float(score), reason, int(n_news), model,
             datetime.now().isoformat()),
        )
        self.conn.commit()

    # ----- generated factors --------------------------------------------

    def save_generated_factor(self, name: str, expr_str: str, train_ic: float,
                              oos_ic: float, accepted: bool,
                              enabled: bool = True) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO generated_factors "
            "(name, expr_str, train_ic, oos_ic, accepted, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, expr_str, _nan_to_none(train_ic), _nan_to_none(oos_ic),
             1 if accepted else 0, 1 if enabled else 0,
             datetime.now().isoformat()),
        )
        self.conn.commit()

    def list_generated_factors(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT name, expr_str, train_ic, oos_ic, accepted, enabled, "
            "created_at FROM generated_factors ORDER BY oos_ic DESC"
        ).fetchall()
        return [{"name": r[0], "expr_str": r[1], "train_ic": r[2],
                 "oos_ic": r[3], "accepted": bool(r[4]), "enabled": bool(r[5]),
                 "created_at": r[6]} for r in rows]

    def set_factor_enabled(self, name: str, enabled: bool) -> None:
        self.conn.execute(
            "UPDATE generated_factors SET enabled=? WHERE name=?",
            (1 if enabled else 0, name))
        self.conn.commit()

    def load_active_factor_fns(self) -> dict:
        from quanti.factors.library import as_factor_fn
        from quanti.factors.parser import FactorParseError, parse_expr
        rows = self.conn.execute(
            "SELECT name, expr_str FROM generated_factors "
            "WHERE accepted=1 AND enabled=1"
        ).fetchall()
        out = {}
        for name, expr_str in rows:
            try:
                out[name] = as_factor_fn(parse_expr(expr_str))
            except FactorParseError:
                logger.warning("skipping unparseable generated factor %s: %s",
                               name, expr_str)
        return out
