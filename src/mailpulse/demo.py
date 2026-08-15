from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .mail.connectors import FakeMailConnector
from .mail.sync import MailSyncService
from .mail.types import RawAttachment, RawMessage
from .models import Mailbox, Task, User
from .security import encrypt_secret


def demo_messages() -> list[RawMessage]:
    now = datetime.now(UTC)
    return [
        RawMessage(
            message_id="<demo-1@mailpulse.local>",
            subject="项目周会与本周行动项",
            sender="project@example.com",
            recipients=["demo@example.com"],
            cc=[],
            received_at=now - timedelta(hours=2),
            body_text="请在周五前确认测试排期。王工负责整理风险清单，下周一进行评审。",
            thread_key="<demo-thread@mailpulse.local>",
        ),
        RawMessage(
            message_id="<demo-2@mailpulse.local>",
            subject="发票附件与付款截止日期",
            sender="finance@example.com",
            recipients=["demo@example.com"],
            cc=[],
            received_at=now - timedelta(hours=5),
            body_text="请查收附件。付款截止日期为本月 28 日。",
            thread_key=None,
            attachments=[
                RawAttachment(
                    filename="invoice.txt",
                    mime_type="text/plain",
                    payload="发票号：INV-2026-001\n金额：1200 元\n付款截止：2026-08-28".encode(),
                )
            ],
        ),
    ]


def seed_demo(session: Session, user: User, data_dir: Path) -> int:
    mailbox = session.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
    if mailbox is None:
        demo_email = f"{user.username}@demo.local"
        mailbox = Mailbox(
            user_id=user.id,
            name="演示邮箱",
            email_address=demo_email,
            imap_host="demo.local",
            smtp_host="demo.local",
            username=demo_email,
            credential_encrypted=encrypt_secret("demo-password"),
        )
        session.add(mailbox)
        session.flush()
    task = session.scalar(select(Task).where(Task.user_id == user.id))
    if task is None:
        task = Task(
            user_id=user.id,
            mailbox_id=mailbox.id,
            name="演示任务",
            run_mode="manual",
            is_enabled=True,
        )
        session.add(task)
        session.flush()
    result = MailSyncService(session).sync(mailbox, FakeMailConnector(demo_messages()))
    return result.created
