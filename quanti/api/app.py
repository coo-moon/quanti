"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from quanti.data.database import Database
from quanti.data.provider import DataProvider


def create_app(
    db: Database | None = None,
    provider: DataProvider | None = None,
    strategies_dir: str | None = "strategies",
) -> FastAPI:
    app = FastAPI(title="Quanti", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store dependencies on app state
    if db is None:
        db = Database()
        db.initialize()
    app.state.db = db
    app.state.provider = provider or DataProvider(db)
    app.state.strategies_dir = strategies_dir

    from quanti.api.routes import router

    app.include_router(router, prefix="/api")

    return app
