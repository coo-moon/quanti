"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from quanti.agent.runtime import AgentRuntime
from quanti.data.background_sync import BackgroundQuoteSyncer
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.factory import make_broker

# Resolve web/dist relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DIST_DIR = _PROJECT_ROOT / "web" / "dist"


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
    # circuit breaker on a ~1min cadence). Live rides xtdata realtime quotes
    # via the bridge; paper rides free Tencent quotes (execution.factory) so
    # intraday stop hits are caught the day they happen instead of after the
    # next daily bar lands. In-session paper SELLs fill immediately at the
    # realtime quote (live-mirror); BUYs and off-session signals queue for
    # the next open as before.
    agent = AgentRuntime(
        db=db, provider=provider, broker=broker,
        strategies_dir=strategies_dir or "strategies",
        screeners_dir=screeners_dir or "screeners",
        intraday_guard_sec=60)
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
            return len(rescored)
        mined = factor_miner.mine_factors(
            llm, db, provider, codes, today, n_candidates=12)
        return len(rescored) + len(mined)

    bg_sync = BackgroundQuoteSyncer(db=db, financials_fn=_sync_latest_financials,
                                    mining_fn=_auto_mine_factors)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        from quanti.agent.goal import load_goal
        try:
            goal = load_goal(db)
            if autostart_agent or goal.enabled:
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

    app = FastAPI(title="Quanti", version="0.2.0", lifespan=_lifespan)

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
