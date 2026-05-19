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
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
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

            CREATE TABLE IF NOT EXISTS stock_pools (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                description TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS pool_stocks (
                pool_name TEXT NOT NULL,
                code TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (pool_name, code)
            );

            CREATE TABLE IF NOT EXISTS sync_jobs (
                job_id TEXT PRIMARY KEY,
                pool_name TEXT NOT NULL,
                total INTEGER NOT NULL,
                current INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                errors_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            -- Live / paper-trading state ---------------------------------

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
                updated_at TEXT NOT NULL
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
                filled_at TEXT
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

            CREATE INDEX IF NOT EXISTS idx_daily_quotes_code
                ON daily_quotes(code);
            CREATE INDEX IF NOT EXISTS idx_daily_quotes_date
                ON daily_quotes(date);
            CREATE INDEX IF NOT EXISTS idx_trades_date
                ON trades(trade_date);
            CREATE INDEX IF NOT EXISTS idx_orders_created
                ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_decisions_ts
                ON agent_decisions(ts);
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
            SELECT s.code, s.name, s.exchange, s.list_date, s.industry
            FROM pool_stocks ps
            JOIN stocks s ON ps.code = s.code
            WHERE ps.pool_name = ?
            ORDER BY ps.added_at DESC
            """,
            (pool_name,),
        ).fetchall()
        return [
            StockInfo(code=r[0], name=r[1], exchange=r[2], list_date=date.fromisoformat(r[3]), industry=r[4])
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
                        current_price: float, buy_date: date | None) -> None:
        from datetime import datetime
        self.conn.execute(
            """
            INSERT INTO positions (code, quantity, avg_cost, current_price, buy_date, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                quantity=excluded.quantity,
                avg_cost=excluded.avg_cost,
                current_price=excluded.current_price,
                buy_date=excluded.buy_date,
                updated_at=excluded.updated_at
            """,
            (code, quantity, avg_cost, current_price,
             buy_date.isoformat() if buy_date else None,
             datetime.now().isoformat()),
        )
        self.conn.commit()

    def delete_position(self, code: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE code=?", (code,))
        self.conn.commit()

    def list_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT code, quantity, avg_cost, current_price, buy_date, updated_at FROM positions"
        ).fetchall()
        return [
            {
                "code": r[0], "quantity": r[1], "avg_cost": r[2],
                "current_price": r[3],
                "buy_date": date.fromisoformat(r[4]) if r[4] else None,
                "updated_at": r[5],
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
                                reason, created_at, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["order_id"], order["code"], order["direction"],
                order["quantity"], order["price_type"], order.get("limit_price", 0),
                order["status"], order.get("strategy_name", ""),
                order.get("filled_price", 0), order.get("filled_quantity", 0),
                order.get("reason", ""),
                order.get("created_at") or datetime.now().isoformat(),
                order.get("filled_at"),
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

    def list_orders(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT order_id, code, direction, quantity, status, filled_price, "
            "filled_quantity, strategy_name, reason, created_at, filled_at "
            "FROM orders ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "order_id": r[0], "code": r[1], "direction": r[2],
                "quantity": r[3], "status": r[4], "filled_price": r[5],
                "filled_quantity": r[6], "strategy_name": r[7],
                "reason": r[8], "created_at": r[9], "filled_at": r[10],
            }
            for r in rows
        ]

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
