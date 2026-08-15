from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import bootstrap_database
from .logging_config import configure_logging
from .web.routes import router


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    bootstrap = bootstrap_database(settings)
    if bootstrap is not None:
        console_logger = logger.bind(console_only=True)
        console_logger.info("MailPulse 已初始化默认管理员账号")
        console_logger.info("默认管理员邮箱: {}", bootstrap.email)
        console_logger.info("默认管理员密码: {}", bootstrap.password)
        console_logger.info("首次登录后可在账号设置中修改密码。")
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
