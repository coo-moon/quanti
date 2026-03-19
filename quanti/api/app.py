"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from quanti.data.database import Database
from quanti.data.provider import DataProvider

# Resolve web/dist relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DIST_DIR = _PROJECT_ROOT / "web" / "dist"


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

    # Serve Vue frontend static files
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
