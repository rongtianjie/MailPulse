from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import bootstrap_database
from .web.routes import router


def create_app() -> FastAPI:
    settings = get_settings()
    bootstrap = bootstrap_database(settings)
    if bootstrap is not None:
        print("MailPulse 已初始化默认管理员账号", flush=True)
        print(f"默认管理员邮箱: {bootstrap.email}", flush=True)
        print(f"默认管理员密码: {bootstrap.password}", flush=True)
        print("首次登录后可在账号设置中修改密码。", flush=True)
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
