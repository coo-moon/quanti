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
from quanti.execution.paper_broker import PaperBroker

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
    if db is None:
        # Paper account by default; trading state in data/paper.db, market
        # data shared in data/market.db. The live account (data/live.db,
        # same market DB) is wired when QmtBroker lands — real money never
        # shares a file with paper.
        import os
        account = os.environ.get("QUANTI_ACCOUNT", "paper")
        db = Database(f"data/{account}.db", market_db_path="data/market.db")
        db.initialize()
    provider = provider or DataProvider(db)
    # Production fill mode is "pending" — signals queue and fill at the
    # next trading bar's open. See docs/plans/2026-06-01-order-lifecycle.md.
    # Backtest engine + unit tests keep using the "immediate" default of
    # PaperBroker so their assertions on synchronous fills still hold.
    broker = PaperBroker(db=db, provider=provider, initial_cash=initial_cash,
                         fill_mode="pending",
                         strategies_dir=strategies_dir or "strategies")
    agent = AgentRuntime(
        db=db, provider=provider, broker=broker,
        strategies_dir=strategies_dir or "strategies",
        screeners_dir=screeners_dir or "screeners")
    # Independent daemon that keeps daily_quotes fresh, decoupled from the
    # agent's 4h tick. Auto-starts by default so cold-start users don't
    # have to know about it; tests pass autostart_background_sync=False
    # to keep test runs hermetic.
    bg_sync = BackgroundQuoteSyncer(db=db)

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
