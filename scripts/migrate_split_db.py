"""One-shot migration: split the monolithic data/quanti.db into a shared
market DB + a per-account trading DB.

Before:  data/quanti.db        (everything)
After:    data/market.db       (stocks, daily_quotes, calendar, pools,
                                sync_jobs, news_sentiment — shared)
          data/paper.db        (portfolio_state, positions, orders, trades,
                                snapshots, agent_goal, agent_decisions)

The old quanti.db is backed up to data/quanti.db.bak and left untouched.
Idempotent-ish: refuses to clobber non-empty target trading tables.

Usage:  python scripts/migrate_split_db.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quanti.data.database import Database  # noqa: E402

MARKET_TABLES = ["stocks", "daily_quotes", "trade_calendar", "stock_pools",
                 "pool_stocks", "sync_jobs", "news_sentiment"]
TRADING_TABLES = ["portfolio_state", "positions", "orders", "trades",
                  "portfolio_snapshots", "agent_goal", "agent_decisions"]

OLD = "data/quanti.db"
MARKET = "data/market.db"
PAPER = "data/paper.db"


def _copy(conn: sqlite3.Connection, src_alias: str, dst_alias: str,
          tables: list[str]) -> None:
    for t in tables:
        # Source table may not exist on very old DBs — skip gracefully.
        exists = conn.execute(
            f"SELECT 1 FROM {src_alias}.sqlite_master "
            "WHERE type='table' AND name=?", (t,)).fetchone()
        if not exists:
            print(f"  - {t}: not in source, skip")
            continue
        n = conn.execute(f"INSERT INTO {dst_alias}.{t} "
                         f"SELECT * FROM {src_alias}.{t}").rowcount
        print(f"  - {t}: copied {n} rows")


def main() -> int:
    if not Path(OLD).exists():
        print(f"找不到 {OLD},无需迁移(全新环境直接起服务即可)。")
        return 0
    if Path(PAPER).exists() or Path(MARKET).exists():
        print(f"{PAPER} 或 {MARKET} 已存在 — 看来迁移过了。中止以防覆盖。")
        return 1

    print(f"备份 {OLD} → {OLD}.bak")
    shutil.copy2(OLD, OLD + ".bak")

    # Create both targets with the correct split schema (empty tables).
    print("建立 market.db + paper.db 的空表结构...")
    db = Database(PAPER, market_db_path=MARKET)
    db.initialize()
    db.close()

    # Copy data from the old monolith into the right place.
    conn = sqlite3.connect(PAPER)
    conn.execute("ATTACH DATABASE ? AS market", (MARKET,))
    conn.execute("ATTACH DATABASE ? AS old", (OLD,))
    print("拷贝行情表 → market.db:")
    _copy(conn, "old", "market", MARKET_TABLES)
    print("拷贝交易表 → paper.db (main):")
    _copy(conn, "old", "main", TRADING_TABLES)
    conn.commit()

    # Report.
    dq = conn.execute("SELECT COUNT(*) FROM market.daily_quotes").fetchone()[0]
    pos = conn.execute("SELECT COUNT(*) FROM main.positions").fetchone()[0]
    ords = conn.execute("SELECT COUNT(*) FROM main.orders").fetchone()[0]
    conn.close()
    print(f"\n完成。market.daily_quotes={dq} 行 | paper.positions={pos} | "
          f"paper.orders={ords}")
    print(f"旧库保留在 {OLD}.bak。服务改用 paper.db + market.db 后即生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
