from __future__ import annotations

import argparse
import os

from loguru import logger

from .auth import create_user
from .config import get_settings
from .db import bootstrap_database, build_session_factory, init_database, reset_database
from .demo import seed_demo
from .logging_config import configure_logging
from .models import User


def main() -> None:
    parser = argparse.ArgumentParser(prog="mailpulse", description="MailPulse 内网邮件整理工具")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动网页服务")
    serve.add_argument("--host", default=None, help="临时覆盖 MAILPULSE_HOST")
    serve.add_argument("--port", type=int, default=None, help="临时覆盖 MAILPULSE_PORT")
    serve.add_argument("--reload", action="store_true")

    init = subparsers.add_parser("init", help="初始化数据库和管理员账号")
    init.add_argument(
        "--admin-username",
        default=None,
        help="管理员用户名，默认使用 MAILPULSE_DEFAULT_ADMIN_USERNAME",
    )
    init.add_argument("--admin-email", default=None, help="管理员邮箱（可选）")
    init.add_argument("--admin-password", required=True)
    init.add_argument("--display-name", default="系统管理员")
    init.add_argument("--demo", action="store_true", help="同时写入演示邮件")

    subparsers.add_parser("init-db", help="初始化数据库并创建默认管理员账号")
    reset = subparsers.add_parser("reset-db", help="重置 SQLite 数据库并创建默认管理员账号")
    reset.add_argument("--confirm", action="store_true", help="确认删除当前数据库")

    seed = subparsers.add_parser("seed-demo", help="为指定用户写入演示邮件")
    seed.add_argument("--username", required=True)

    run_once = subparsers.add_parser("run-once", help="为指定用户生成一次报告")
    run_once.add_argument("--username", required=True)
    run_once.add_argument("--demo-ai", action="store_true")
    subparsers.add_parser("worker", help="启动后台任务 worker")

    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings)
    if args.command == "serve":
        import uvicorn

        host = args.host if args.host is not None else settings.host
        port = args.port if args.port is not None else settings.port
        host_source = _configuration_source(settings, "host", "MAILPULSE_HOST", args.host)
        port_source = _configuration_source(settings, "port", "MAILPULSE_PORT", args.port)
        logger.info(
            "MailPulse 服务监听地址: {}:{}（host 来源: {}，port 来源: {}）",
            host,
            port,
            host_source,
            port_source,
        )
        uvicorn.run(
            "mailpulse.app:create_app",
            host=host,
            port=port,
            reload=args.reload,
            factory=True,
            log_config=None,
        )
        return
    if args.command == "reset-db":
        if not args.confirm:
            parser.error("reset-db 是破坏性操作，请同时提供 --confirm")
        database_path = reset_database(settings)
        bootstrap = bootstrap_database(settings)
        logger.info("已重置数据库: {}", database_path)
        _log_bootstrap_credentials(bootstrap)
        return
    if args.command == "init":
        init_database(settings)
    else:
        bootstrap = bootstrap_database(settings)
        if args.command == "init-db":
            logger.info("数据库已初始化。")
            _log_bootstrap_credentials(bootstrap)
            return
    factory = build_session_factory(settings)
    db = factory()
    try:
        if args.command == "init":
            username = args.admin_username or settings.default_admin_username
            user = create_user(
                db,
                username,
                args.admin_password,
                args.display_name,
                email=args.admin_email,
                role="admin",
            )
            if args.demo:
                seed_demo(db, user, settings.data_dir)
            db.commit()
            logger.info("已创建管理员: {}", user.username)
            return
        if args.command == "seed-demo":
            user = db.query(User).filter(User.username == args.username.strip().lower()).one()
            created = seed_demo(db, user, settings.data_dir)
            db.commit()
            logger.info("已写入演示邮件: {} 封", created)
            return
        if args.command == "run-once":
            from .report_service import ReportService

            user = db.query(User).filter(User.username == args.username.strip().lower()).one()
            report = ReportService(db, settings).generate_for_user(
                user, use_demo_provider=args.demo_ai
            )
            db.commit()
            logger.info("已生成报告: {}", report.id)
            return
        if args.command == "worker":
            db.close()
            from .worker import run_worker

            run_worker(settings)
            return
    finally:
        db.close()
    parser.print_help()


def _log_bootstrap_credentials(bootstrap) -> None:
    console_logger = logger.bind(console_only=True)
    if bootstrap is None:
        console_logger.info("默认管理员账号已存在，未输出密码。")
        return
    console_logger.info("默认管理员用户名: {}", bootstrap.username)
    if bootstrap.email:
        console_logger.info("默认管理员邮箱: {}", bootstrap.email)
    console_logger.info("默认管理员密码: {}", bootstrap.password)
    console_logger.info("首次登录后可在账号设置中修改密码。")


def _configuration_source(settings, field_name: str, env_name: str, command_value) -> str:
    if command_value is not None:
        return "命令行"
    if env_name in os.environ:
        return "环境变量"
    if field_name in settings.model_fields_set:
        return ".env"
    return "代码默认值"
