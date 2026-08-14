"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from quanti.agent.runtime import AgentRuntime
from quanti.data.background_sync import BackgroundQuoteSyncer
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.factory import make_broker
from quanti.utils.jsonsafe import json_safe

# Resolve web/dist relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DIST_DIR = _PROJECT_ROOT / "web" / "dist"

logger = logging.getLogger(__name__)

# Deadlock/postmortem observability: when QUANTI_STACK_DUMP=1 the server
# registers a SIGUSR1 handler that dumps EVERY thread's Python stack (with
# exception chains) to stderr — the launchd error log. Trigger it any time
# with `kill -USR1 <pid>`; nothing is dumped unless the signal arrives, so
# the knob is free to leave ON in supervised deployments.
def _register_stack_dump() -> None:
    import faulthandler
    import os
    import signal

    if os.environ.get("QUANTI_STACK_DUMP") != "1":
        return
    faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)
    logger.info("stack dump armed: kill -USR1 %s for full-thread tracebacks",
                os.getpid())


_register_stack_dump()


class SafeJSONResponse(JSONResponse):
    """响应里的 NaN/±Inf 渲染成 `null`,而不是让整个端点 500。

    starlette 的 JSONResponse 用 `allow_nan=False` 序列化(合规做法:裸 NaN
    不是合法 JSON,前端 JSON.parse 也解不了),于是**任何一个**混进 NaN 的字段
    都会把整条响应打成 500。这在本项目已经发生过两次、且在两个不同端点
    (/api/regime/* 与 /api/agent/decisions),数据源头各不相同 —— 说明这是边界
    问题,不该在每个模块各修一次。

    先按原样严格序列化,**只有**抛 ValueError 时才做一次净化重试:正常响应
    (绝大多数)零额外开销,不会给 /api/stocks 这类大响应白加一遍递归。

    注意这是最后一道兜底,不是许可证 —— 数据仍应在落库前净化(见
    quanti.utils.jsonsafe 的模块说明),否则库里会攒下解析不了的非法 JSON。
    """

    def render(self, content) -> bytes:
        try:
            return super().render(content)
        except ValueError:
            logger.warning("响应含非有限浮点(NaN/Inf),已降级为 null 后返回")
            return super().render(json_safe(content))


def create_app(
    db: Database | None = None,
    provider: DataProvider | None = None,
    strategies_dir: str | None = "strategies",
    screeners_dir: str | None = "screeners",
    initial_cash: float = 1_000_000.0,
    autostart_agent: bool = False,
    autostart_background_sync: bool = True,
) -> FastAPI:
    # Which account this process serves (paper / live). Determines the
    # trading DB AND drives the UI's 模拟盘/实盘 badge so real money is never
    # mistaken for paper. Default 'paper'.
    import os
    account = os.environ.get("QUANTI_ACCOUNT", "paper")
    if db is None:
        # Trading state in data/{account}.db, market data shared in
        # data/market.db. Real money never shares a file with paper.
        db = Database(f"data/{account}.db", market_db_path="data/market.db")
        db.initialize()
    provider = provider or DataProvider(db)
    # Broker per account: live → QmtBroker(require_live) over qmt-bridge, else
    # PaperBroker. Production paper fill mode is "pending" — signals queue and
    # fill at the next trading bar's open (docs/plans/2026-06-01-order-lifecycle).
    # Backtest engine + unit tests keep PaperBroker's "immediate" default.
    broker = make_broker(db, provider, account=account,
                         initial_cash=initial_cash, fill_mode="pending",
                         strategies_dir=strategies_dir or "strategies")
    # Both accounts run the fast intraday guard (reconcile fills + exits +
    # circuit breaker). Cadence is env-tunable via QUANTI_INTRADAY_GUARD_SEC
    # (default 5s) — the guard waits this long BETWEEN cycles (sequential, no
    # overlap), so a lower value = faster fill-reconcile + stop-loss reaction.
    # Live rides xtdata realtime quotes via the bridge; paper rides free Tencent
    # quotes (execution.factory) so intraday stop hits are caught the day they
    # happen instead of after the next daily bar lands. In-session paper SELLs
    # fill immediately at the realtime quote (live-mirror); BUYs and off-session
    # signals queue for the next open as before.
    guard_sec = int(os.environ.get("QUANTI_INTRADAY_GUARD_SEC", "5") or 5)
    agent = AgentRuntime(
        db=db, provider=provider, broker=broker,
        strategies_dir=strategies_dir or "strategies",
        screeners_dir=screeners_dir or "screeners",
        intraday_guard_sec=guard_sec)
    # Independent daemon that keeps daily_quotes fresh, decoupled from the
    # agent's 4h tick. Auto-starts by default so cold-start users don't
    # have to know about it; tests pass autostart_background_sync=False
    # to keep test runs hermetic.
    # Once/day, the daemon also refreshes the latest report period's financials
    # (free akshare 业绩报表) so quality/value factors stay current — quotes and
    # financials kept fresh together.
    # Follow the configured source (tushare VIP → real ann_date), backstopping
    # to free akshare when it can't / returns 0. See source.refresh_latest_financials.
    def _sync_latest_financials() -> int:
        from quanti.data.source import refresh_latest_financials
        return refresh_latest_financials(db)

    # Once/day, the daemon also keeps the generated-factor library current:
    # always re-score existing factors against fresh data (no LLM), and mine a
    # small batch of NEW candidates when an LLM is configured. Small n_candidates
    # so one generation stays under the LLM client read timeout. Off via goal
    # param auto_mine_daily=false; silently re-score-only without an LLM key.
    def _auto_mine_factors() -> int:
        from datetime import date as _date

        from quanti.agent import factor_miner
        from quanti.agent.goal import load_goal
        from quanti.agent.universe import resolve_tradable_universe
        goal = load_goal(db)
        params = goal.params or {}
        if not params.get("auto_mine_daily", True):
            return 0
        today = _date.today()
        codes = resolve_tradable_universe(
            db, provider, pool=goal.universe_pool, params=params, as_of=today)
        rescored = factor_miner.rescore_generated_factors(db, provider, codes, today)
        try:
            from quanti.agent.llm_runtime import build_llm_client
            llm = build_llm_client(params)
        except Exception:  # noqa: BLE001 - no/invalid LLM key → re-score only
            mined = []
        else:
            mined = factor_miner.mine_factors(
                llm, db, provider, codes, today, n_candidates=12)
        # IC drift watch: an edge that dies slowly must not linger silently —
        # decay / gate-retirement / unmonitored factors land in the decision
        # log (Web Agent page) for the operator and the judge LLM to see.
        from quanti.agent.factor_watch import watch_factor_drift
        watch = watch_factor_drift(db)
        problems = ((watch.get("decayed") or []) + (watch.get("newly_rejected") or []))
        unmonitored = watch.get("unmonitored") or []
        if problems or unmonitored:
            bits = []
            if problems:
                bits.append("衰减/退役: " + ", ".join(problems))
            if unmonitored:
                bits.append("无快照(靠信仰交易): " + ", ".join(unmonitored))
            db.log_decision(
                "factor_watch", "因子漂移需关注: " + " | ".join(bits),
                details={k: watch.get(k) for k in
                         ("decayed", "newly_rejected", "newly_accepted",
                          "unmonitored", "as_of")})
        return len(rescored) + len(mined)

    # Once/day after 17:30 (market closed, bars topped up), snapshot the market
    # regime: full-market breadth + sector rotation + news, run through
    # DeepSeek v4-flash in thinking mode, persisted to market.regime_snapshots.
    # Observe-only — it never emits a trade signal. Without DEEPSEEK_API_KEY the
    # data layer still lands and only the narrative is skipped.
    # 返回值不能丢:数据面不可用时 bg_sync 靠 snap["usable"] 决定当天要不要
    # 重试(17:30 可能撞上当天行情还没同步完 —— 2026-08-06 就是)。
    def _daily_regime() -> dict:
        from quanti.regime import report as regime_report
        return regime_report.generate(db)

    # Once/day after 17:45 (regime done, today's bars landed), run the doctor
    # (exit coverage + data freshness + DB integrity) and persist findings as
    # decision entries — so health problems show up in the audit trail without
    # anyone having to remember `quanti doctor`. Read-only; never trades.
    def _daily_doctor() -> dict:
        from quanti.health import run_doctor
        report = run_doctor(db, strategies_dir=strategies_dir)
        checks = report.get("checks", {})
        problems = [f"{name}: {c.get('detail', '')}"
                    for name, c in checks.items() if not c.get("ok")]
        if problems:
            db.log_decision(
                "doctor_warn", "每日体检发现问题: " + " | ".join(problems),
                details=report)
        else:
            db.log_decision("doctor_ok", "每日体检通过", details=report)
        return report

    # Once/day after the doctor: the strategy health gate — a 2-year backtest
    # per loadable strategy under DEFAULT risk. Verdicts land in strategy_gate;
    # breaker/deep_loss strategies are excluded from the selector, so the
    # walk-forward can never re-admit an account-killer (2026-08-14 erratum:
    # every built-in strategy tripped -30% within 1-2 years). Read-only.
    def _daily_strategy_gate() -> dict:
        from quanti.agent.strategy_gate import compute_gate
        report = compute_gate(db, provider, strategies_dir=strategies_dir)
        excluded = [f"{name}({g.get('verdict')})"
                    for name, g in report.items()
                    if g.get("verdict") in ("breaker", "deep_loss")]
        if excluded:
            db.log_decision(
                "strategy_gate", "策略闸门剔除: " + ", ".join(excluded),
                details=report)
        return report

    # Heavy daily hooks (doctor / strategy gate / factor re-score) defer for
    # QUANTI_HOOK_WARMUP_SEC (default 1800s) after boot so they never pile on
    # top of the agent's cold first-tick selector sweep (2026-08-14 rounds 8-9
    # diagnosis: the overlap was the lock convoy). 0 disables.
    import os as _os
    try:
        _warmup = float(_os.environ.get("QUANTI_HOOK_WARMUP_SEC", "1800"))
    except ValueError:
        _warmup = 1800.0
    bg_sync = BackgroundQuoteSyncer(db=db, financials_fn=_sync_latest_financials,
                                    mining_fn=_auto_mine_factors,
                                    regime_fn=_daily_regime,
                                    doctor_fn=_daily_doctor,
                                    strategy_gate_fn=_daily_strategy_gate,
                                    heavy_warmup_sec=_warmup)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        from quanti.agent.goal import load_goal
        try:
            goal = load_goal(db)
            # Live never auto-starts the agent on boot — real money must not be
            # traded by merely starting the process (or auto-resuming across a
            # restart). The operator explicitly hits agent_start (Web/MCP/CLI)
            # each session. Paper keeps the boot-time autostart. (H3)
            if account == "live":
                if autostart_agent or goal.enabled:
                    logger.warning(
                        "实盘模式:不在启动时自动拉起 Agent,请手动 agent_start "
                        "(goal.enabled=%s 被忽略)", goal.enabled)
            elif autostart_agent or goal.enabled:
                agent.start()
        except Exception:
            pass
        if autostart_background_sync:
            try:
                bg_sync.start()
            except Exception:
                pass
        yield
        # Process shutdown — halt the thread but do NOT flip the goal back
        # to disabled, otherwise the agent would never auto-resume across
        # server restarts. User-driven stop goes through agent.stop().
        try:
            agent.shutdown()
        except Exception:
            pass
        try:
            bg_sync.shutdown(timeout=3.0)
        except Exception:
            pass

    app = FastAPI(title="Quanti", version="0.2.0", lifespan=_lifespan,
                  default_response_class=SafeJSONResponse)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.db = db
    app.state.account = account
    app.state.provider = provider
    app.state.strategies_dir = strategies_dir
    app.state.screeners_dir = screeners_dir
    app.state.broker = broker
    app.state.agent = agent
    app.state.bg_sync = bg_sync

    from quanti.api.routes import router

    app.include_router(router, prefix="/api")

    if _DIST_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=_DIST_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            """Serve Vue SPA - fallback to index.html for client-side routing."""
            file_path = _DIST_DIR / full_path
            if file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(_DIST_DIR / "index.html")

    return app
