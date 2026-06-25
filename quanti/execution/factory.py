"""Pick the broker for the running account.

One switch point so app.py / cli.py never diverge on the paper↔live decision:
- account == "live"  → QmtBroker over the qmt-bridge with require_live=True, so
  a silent mock-bridge fallback reads as NOT connected and never phantom-fills.
- otherwise           → PaperBroker (simulation).
"""

from __future__ import annotations

import os


def make_broker(db, provider, *, account: str | None = None,
                initial_cash: float = 1_000_000.0,
                strategies_dir: str = "strategies",
                fill_mode: str = "pending"):
    """Build the account's broker. `account` defaults to env QUANTI_ACCOUNT.
    `fill_mode` is a passthrough for the paper path (ignored for live)."""
    acct = account if account is not None else os.environ.get("QUANTI_ACCOUNT", "paper")
    if acct == "live":
        from quanti.execution.qmt_broker import QmtBroker
        return QmtBroker(db, provider, initial_cash=initial_cash,
                         strategies_dir=strategies_dir, require_live=True)
    from quanti.execution.paper_broker import PaperBroker
    return PaperBroker(db=db, provider=provider, initial_cash=initial_cash,
                       fill_mode=fill_mode, strategies_dir=strategies_dir)
