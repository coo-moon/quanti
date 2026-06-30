from __future__ import annotations

from datetime import date, datetime, timedelta

from quanti.data.database import Database


def _order(db, *, code, direction, status, strategy, reason, filled_at):
    db.insert_order({
        "order_id": "o_" + code + direction + (filled_at or "x"),
        "code": code, "direction": direction, "quantity": 100,
        "price_type": "market", "limit_price": 0.0, "status": status,
        "strategy_name": strategy, "filled_price": 9.0, "filled_quantity": 100,
        "reason": reason, "created_at": datetime.now().isoformat(),
        "filled_at": filled_at, "entry_strategy": "",
    })


def test_stop_loss_exit_dates_filters_correctly(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.ensure_portfolio(100_000)
    today = date(2026, 6, 20)

    def iso(d: date) -> str:
        return datetime(d.year, d.month, d.day, 15, 0).isoformat()
    # A real stop-loss exit (counts).
    _order(db, code="000001", direction="sell", status="filled",
           strategy="risk_exit", reason="止损 -10.0% ≤ -8.0%",
           filled_at=iso(date(2026, 6, 18)))
    # Take-profit exit (excluded — wrong reason).
    _order(db, code="000002", direction="sell", status="filled",
           strategy="risk_exit", reason="移动止盈 浮盈+20%",
           filled_at=iso(date(2026, 6, 18)))
    # A BUY (excluded — wrong direction).
    _order(db, code="000003", direction="buy", status="filled",
           strategy="ma_cross", reason="买入信号",
           filled_at=iso(date(2026, 6, 18)))
    # Unfilled stop intent (excluded — not filled).
    _order(db, code="000004", direction="sell", status="pending",
           strategy="risk_exit", reason="止损 -9% ≤ -8%", filled_at=None)
    # Too old (excluded by `since`).
    _order(db, code="000005", direction="sell", status="filled",
           strategy="risk_exit", reason="止损 -9% ≤ -8%",
           filled_at=iso(date(2026, 5, 1)))

    dates = db.stop_loss_exit_dates(since=today - timedelta(days=10))
    assert dates == [date(2026, 6, 18)]


def test_pragmas_tuned_on_both_schemas(tmp_path):
    # 性能 PRAGMA 是 per-database：ATTACH 的 market 库必须单独设，不能只设 main
    # （回归守卫：曾经只 `PRAGMA journal_mode=WAL`，sync/cache/mmap 全是默认）。
    db = Database(str(tmp_path / "acc.db"),
                  market_db_path=str(tmp_path / "mkt.db"))
    db.initialize()
    try:
        for schema in ("main", "market"):
            c = db.conn
            assert c.execute(f"PRAGMA {schema}.journal_mode").fetchone()[0] == "wal"
            assert c.execute(f"PRAGMA {schema}.synchronous").fetchone()[0] == 1       # NORMAL，WAL 下 crash-safe
            assert c.execute(f"PRAGMA {schema}.cache_size").fetchone()[0] == -262144  # 256 MB
            assert c.execute(f"PRAGMA {schema}.mmap_size").fetchone()[0] == 268435456  # 256 MB
    finally:
        db.close()
