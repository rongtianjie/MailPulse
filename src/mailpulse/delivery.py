from __future__ import annotations

import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Protocol

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import AuditLog, Delivery, Mailbox, Report, utc_now
from .security import decrypt_secret


class DeliveryProvider(Protocol):
    def send(self, sender: str, recipient: str, subject: str, body: str) -> None: ...


@dataclass(slots=True)
class SMTPConfig:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool = True


class SMTPDeliveryProvider:
    def __init__(self, config: SMTPConfig):
        self.config = config

    def send(self, sender: str, recipient: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = _safe_subject(subject)
        message.set_content(body)
        if self.config.use_tls and self.config.port == 465:
            with smtplib.SMTP_SSL(self.config.host, self.config.port, timeout=20) as client:
                client.login(self.config.username, self.config.password)
                client.send_message(message)
            return
        with smtplib.SMTP(self.config.host, self.config.port, timeout=20) as client:
            if self.config.use_tls:
                client.starttls()
            client.login(self.config.username, self.config.password)
            client.send_message(message)


class ReportDeliveryService:
    """Create an auditable delivery record without persisting content or secrets."""

    def __init__(self, session: Session, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    def send_report(
        self,
        report: Report,
        mailbox: Mailbox,
        recipient: str,
        provider: DeliveryProvider | None = None,
    ) -> Delivery:
        destination = normalize_recipient(recipient)
        delivery = Delivery(
            report_id=report.id,
            channel="smtp",
            destination=destination,
            status="pending",
        )
        self.session.add(delivery)
        self.session.flush()
        self._attempt(delivery, report, mailbox, provider)
        self.session.flush()
        self.session.add(
            AuditLog(
                actor_user_id=report.user_id,
                action="report_delivery",
                target_type="delivery",
                target_id=str(delivery.id),
                metadata_json={
                    "channel": delivery.channel,
                    "status": delivery.status,
                    "attempts": delivery.attempts,
                },
            )
        )
        return delivery

    def retry_delivery(
        self,
        delivery: Delivery,
        report: Report,
        mailbox: Mailbox,
        provider: DeliveryProvider | None = None,
    ) -> Delivery:
        if delivery.channel != "smtp":
            raise ValueError("当前仅支持 SMTP 投递")
        if delivery.status == "sent":
            return delivery
        self._attempt(delivery, report, mailbox, provider)
        self.session.flush()
        self.session.add(
            AuditLog(
                actor_user_id=report.user_id,
                action="report_delivery_retry",
                target_type="delivery",
                target_id=str(delivery.id),
                metadata_json={
                    "channel": delivery.channel,
                    "status": delivery.status,
                    "attempts": delivery.attempts,
                },
            )
        )
        return delivery

    def _attempt(
        self,
        delivery: Delivery,
        report: Report,
        mailbox: Mailbox,
        provider: DeliveryProvider | None,
    ) -> None:
        delivery.status = "sending"
        delivery.attempts += 1
        try:
            if provider is None:
                password = decrypt_secret(mailbox.credential_encrypted, self.settings)
                provider = SMTPDeliveryProvider(
                    SMTPConfig(
                        host=mailbox.smtp_host,
                        port=mailbox.smtp_port,
                        username=mailbox.username,
                        password=password,
                        use_tls=mailbox.smtp_tls,
                    )
                )
            provider.send(
                mailbox.email_address,
                delivery.destination,
                report.title,
                report.rendered_markdown,
            )
        except Exception as exc:
            delivery.status = "failed"
            delivery.error_message = f"{type(exc).__name__}: SMTP 投递失败"
            delivery.sent_at = None
            return
        delivery.status = "sent"
        delivery.error_message = None
        delivery.sent_at = utc_now()


def normalize_recipient(value: str) -> str:
    addresses = getaddresses([value.strip()])
    if len(addresses) != 1:
        raise ValueError("收件人必须是一个有效邮箱地址")
    _name, address = addresses[0]
    if (
        not address
        or len(address) > 320
        or "\r" in address
        or "\n" in address
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address)
    ):
        raise ValueError("收件人必须是一个有效邮箱地址")
    return address


def _safe_subject(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:255]
