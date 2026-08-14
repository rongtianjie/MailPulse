from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import build_engine, init_database
from .search import SearchService
from .web.routes import router


def create_app() -> FastAPI:
    settings = get_settings()
    init_database(settings)
    engine = build_engine(settings)
    SearchService.ensure_index(engine)
    app = FastAPI(title="MailPulse", version="0.1.0")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie,
        https_only=settings.session_https_only,
        same_site="lax",
    )

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/healthz", response_class=HTMLResponse)
    def healthz() -> str:
        return "ok"

    app.include_router(router)

    return app
