from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import bootstrap_database, build_session_factory
from .logging_config import configure_logging
from .web.routes import router
from .web.session import SessionCookiePolicyMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    bootstrap = bootstrap_database(settings)
    if bootstrap is not None:
        console_logger = logger.bind(console_only=True)
        console_logger.info("MailPulse 已初始化默认管理员账号")
        console_logger.info("默认管理员用户名: {}", bootstrap.username)
        console_logger.info("默认管理员密码: {}", bootstrap.password)
        console_logger.info("首次登录后可在账号设置中修改密码。")
    app = FastAPI(title="MailPulse", version="0.1.0")
    app.state.session_factory = build_session_factory(settings)
    app.state.db_engine = app.state.session_factory.kw["bind"]

    @app.on_event("shutdown")
    def _dispose_db_engine() -> None:
        app.state.db_engine.dispose()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie,
        max_age=settings.remember_me_days * 24 * 60 * 60,
        https_only=settings.session_https_only,
        same_site="lax",
    )
    app.add_middleware(
        SessionCookiePolicyMiddleware,
        session_cookie=settings.session_cookie,
    )

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/healthz", response_class=HTMLResponse)
    def healthz() -> str:
        return "ok"

    app.include_router(router)

    return app
