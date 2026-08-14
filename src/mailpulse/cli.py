from __future__ import annotations

import argparse

from .auth import create_user
from .config import get_settings
from .db import build_session_factory, init_database
from .demo import seed_demo
from .models import User


def main() -> None:
    parser = argparse.ArgumentParser(prog="mailpulse", description="MailPulse 内网邮件整理工具")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动网页服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--reload", action="store_true")

    init = subparsers.add_parser("init", help="初始化数据库和管理员账号")
    init.add_argument("--admin-email", required=True)
    init.add_argument("--admin-password", required=True)
    init.add_argument("--display-name", default="系统管理员")
    init.add_argument("--demo", action="store_true", help="同时写入演示邮件")

    seed = subparsers.add_parser("seed-demo", help="为指定用户写入演示邮件")
    seed.add_argument("--user-email", required=True)

    run_once = subparsers.add_parser("run-once", help="为指定用户生成一次报告")
    run_once.add_argument("--user-email", required=True)
    run_once.add_argument("--demo-ai", action="store_true")
    subparsers.add_parser("worker", help="启动后台任务 worker")

    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "mailpulse.app:create_app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            factory=True,
        )
        return
    settings = get_settings()
    init_database(settings)
    factory = build_session_factory(settings)
    db = factory()
    try:
        if args.command == "init":
            user = create_user(
                db, args.admin_email, args.admin_password, args.display_name, role="admin"
            )
            if args.demo:
                seed_demo(db, user, settings.data_dir)
            db.commit()
            print(f"已创建管理员: {user.email}")
            return
        if args.command == "seed-demo":
            user = db.query(User).filter(User.email == args.user_email.strip().lower()).one()
            created = seed_demo(db, user, settings.data_dir)
            db.commit()
            print(f"已写入演示邮件: {created} 封")
            return
        if args.command == "run-once":
            from .report_service import ReportService

            user = db.query(User).filter(User.email == args.user_email.strip().lower()).one()
            report = ReportService(db, settings).generate_for_user(
                user, use_demo_provider=args.demo_ai
            )
            db.commit()
            print(f"已生成报告: {report.id}")
            return
        if args.command == "worker":
            raise SystemExit("worker 调度器尚未接入，请先使用 run-once 验证任务链路")
    finally:
        db.close()
    parser.print_help()
