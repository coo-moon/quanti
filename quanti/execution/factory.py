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


def _local_utc_offset():
    """The host's current UTC offset (a timedelta). Isolated so tests can
    simulate a UTC / wrong-tz host without touching the real clock."""
    from datetime import datetime
    return datetime.now().astimezone().utcoffset()


def _assert_cn_timezone() -> None:
    """Live A-share trading sessions are computed from a naive ``datetime.now()``
    (host local time — see ``quanti/utils/market.py``). So the host MUST be on
    Beijing time (UTC+8): on a UTC cloud/VM the 09:30–15:00 window maps to
    ~01:30–07:00 local, ``in_trading_session`` is False all day, and the intraday
    guard (pending fills, per-stock stop-loss, portfolio circuit breaker) silently
    no-ops during the real session. Fail loud at live startup rather than run a
    whole day with risk control disabled.

    Set QUANTI_ALLOW_NON_CN_TZ=1 only if you have deliberately made now() return
    Beijing time some other way (rare)."""
    import os
    from datetime import timedelta
    if os.environ.get("QUANTI_ALLOW_NON_CN_TZ", "") == "1":
        return
    off = _local_utc_offset()
    if off != timedelta(hours=8):
        raise RuntimeError(
            "拒绝构建实盘 broker:主机时区必须为北京时间(UTC+8),当前本地 UTC 偏移为 "
            f"{off}。A 股交易时段由本地 naive now() 计算,时区不对会让盘中守护/止损/"
            "熔断在真实交易时段整段空转。请把机器时区设为 Asia/Shanghai 后重试"
            "(若确已用其他方式让 now() 返回北京时间,可设 QUANTI_ALLOW_NON_CN_TZ=1 跳过)。")


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
        _assert_cn_timezone()
        from quanti.execution.qmt_broker import QmtBroker
        return QmtBroker(db, provider, initial_cash=initial_cash,
                         strategies_dir=strategies_dir, require_live=True)
    from quanti.data.realtime import make_realtime_fetcher
    from quanti.execution.paper_broker import PaperBroker
    # Paper marks ride free Tencent quotes during trading sessions (tushare
    # sina 兜底,腾讯退避时接管——见 quanti/data/realtime.py); live rides
    # xtdata via the qmt-bridge.
    return PaperBroker(db=db, provider=provider, initial_cash=initial_cash,
                       fill_mode=fill_mode, strategies_dir=strategies_dir,
                       realtime_quote_fn=make_realtime_fetcher(db))
