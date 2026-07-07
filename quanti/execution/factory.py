"""Pick the broker for the running account.

One switch point so app.py / cli.py never diverge on the paper↔live decision:
- account == "live"  → QmtBroker over the qmt-bridge with require_live=True, so
  a silent mock-bridge fallback reads as NOT connected and never phantom-fills.
- otherwise           → PaperBroker (simulation).
"""

from __future__ import annotations

import os

# Second confirmation for real money: setting QUANTI_ACCOUNT=live alone must NOT
# be enough to route orders to a real broker (a stray env var or a reused script
# would silently trade real money). The operator must ALSO acknowledge intent
# with QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY. This is the one choke point every
# entrypoint (up / serve / agent) funnels through, so the gate can't be bypassed.
LIVE_ACK_ENV = "QUANTI_LIVE_ACK"
LIVE_ACK_TOKEN = "I_KNOW_REAL_MONEY"


def make_broker(db, provider, *, account: str | None = None,
                initial_cash: float = 1_000_000.0,
                strategies_dir: str = "strategies",
                fill_mode: str = "pending"):
    """Build the account's broker. `account` defaults to env QUANTI_ACCOUNT.
    `fill_mode` is a passthrough for the paper path (ignored for live).

    Live requires the explicit ``QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY``
    acknowledgment or it refuses to build a real-money broker (raises)."""
    acct = account if account is not None else os.environ.get("QUANTI_ACCOUNT", "paper")
    if acct == "live":
        if os.environ.get(LIVE_ACK_ENV, "") != LIVE_ACK_TOKEN:
            raise RuntimeError(
                "拒绝构建实盘 broker:QUANTI_ACCOUNT=live 需要二次确认。"
                f"请显式设置 {LIVE_ACK_ENV}={LIVE_ACK_TOKEN} 表示你清楚这是真钱交易。")
        from quanti.execution.qmt_broker import QmtBroker
        return QmtBroker(db, provider, initial_cash=initial_cash,
                         strategies_dir=strategies_dir, require_live=True)
    from quanti.data.tencent_quotes import fetch_last_prices
    from quanti.execution.paper_broker import PaperBroker
    # Paper marks ride free Tencent quotes during trading sessions so the
    # intraday guard sees live prices; live rides xtdata via the qmt-bridge.
    return PaperBroker(db=db, provider=provider, initial_cash=initial_cash,
                       fill_mode=fill_mode, strategies_dir=strategies_dir,
                       realtime_quote_fn=fetch_last_prices)
