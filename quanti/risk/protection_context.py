"""Build a ProtectionContext from the live SQLite state (paper/live brokers).

Kept separate from protections.py so the protection logic stays pure (no DB
import). The backtest builds its own ProtectionContext from in-memory data."""

from __future__ import annotations

from datetime import date, timedelta

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.risk.protections import ProtectionConfig, ProtectionContext
from quanti.utils.market import count_trading_days_between


def build_db_context(db: Database, provider: DataProvider,
                     config: ProtectionConfig,
                     today: date | None = None) -> ProtectionContext:
    """Build a ProtectionContext from live SQLite data for use by live brokers."""
    today = today or date.today()
    # Generous calendar lower bound covering the largest fact window
    # (lock + lookback) in trading days, padded for weekends/holidays.
    span_td = max(config.sg_lock_days + config.sg_lookback_days,
                  config.md_lock_days + config.md_lookback_days)
    since = today - timedelta(days=span_td * 2 + 14)

    sl_dates = db.stop_loss_exit_dates(since)

    equity: list[tuple[date, float]] = []
    for snap in db.get_portfolio_snapshots(limit=span_td * 2 + 14):
        try:
            sd = date.fromisoformat(snap["snapshot_date"])
        except (ValueError, TypeError, KeyError):
            continue
        if sd >= since:
            equity.append((sd, float(snap["total_value"])))
    equity.sort()

    def _trading_days_between(a: date, b: date) -> int:
        return count_trading_days_between(a, b, provider)

    # Trailing daily returns per held name — only when the correlation guard is
    # on (else skip the per-position fetches entirely).
    holdings_returns: dict[str, list[float]] = {}
    if config.correlation_guard_enabled:
        cg_start = today - timedelta(days=config.cg_lookback_days * 2 + 14)
        for pos in db.list_positions():
            code = pos["code"]
            df = provider.get_daily_df(code, cg_start, today)
            if df is None or len(df) < 2:
                continue
            closes = df.sort_values("date")["close"].astype(float)
            rets = closes.pct_change().dropna().tolist()
            if rets:
                holdings_returns[code] = rets

    # Upcoming share-unlocks (one query) — only when the unlock guard is on.
    upcoming_unlocks: dict[str, float] = {}
    if config.unlock_guard_enabled:
        upcoming_unlocks = db.get_upcoming_unlocks(today, config.ug_horizon_days)

    return ProtectionContext(
        today=today,
        stop_loss_exit_dates=sl_dates,
        equity_series=equity,
        trading_days_between=_trading_days_between,
        holdings_returns=holdings_returns,
        upcoming_unlocks=upcoming_unlocks,
    )


def evaluate_entry(risk, protections, db, provider, signal, portfolio):
    """Shared entry gate for the live brokers: RiskManager caps + protections.

    Returns (ok, reason, reject_kind) with reject_kind in
    {"", "risk_reject", "protection_block"}. Protections only gate BUY;
    SELL passes through the risk check unchanged. Both PaperBroker and
    QmtBroker delegate here so the gate logic lives in one place."""
    from quanti.models import Direction
    ok, reason = risk.check(signal, portfolio)
    if not ok:
        return False, reason, "risk_reject"
    if signal.direction == Direction.BUY and protections.config.enabled:
        ctx = build_db_context(db, provider, protections.config)
        allowed, preason = protections.check_entry(ctx, signal.stock_code)
        if not allowed:
            return False, preason, "protection_block"
    return True, "", ""
