from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(512))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str] = mapped_column(String(32), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    mailboxes: Mapped[list[Mailbox]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Mailbox(Base):
    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="默认邮箱")
    email_address: Mapped[str] = mapped_column(String(320))
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    username: Mapped[str] = mapped_column(String(320))
    credential_encrypted: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(String(255), default="INBOX")
    sync_uid_validity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sync_last_uid: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    user: Mapped[User] = relationship(back_populates="mailboxes")


class CanonicalMessage(Base):
    __tablename__ = "canonical_messages"
    __table_args__ = (
        Index("ix_canonical_messages_sender_date", "sender", "received_at"),
        Index("ix_canonical_messages_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str | None] = mapped_column(String(998), nullable=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(Text, default="")
    sender: Mapped[str] = mapped_column(String(998), default="")
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list)
    cc: Mapped[list[str]] = mapped_column(JSON, default=list)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    body_text: Mapped[str] = mapped_column(Text, default="")
    thread_key: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    local_labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    local_starred: Mapped[bool] = mapped_column(Boolean, default=False)
    local_processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    occurrences: Mapped[list[MessageOccurrence]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class MessageOccurrence(Base):
    __tablename__ = "message_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_id", "folder", "uid_validity", "uid", name="uq_mail_occurrence_cursor"
        ),
        Index("ix_message_occurrences_mailbox", "mailbox_id", "folder", "uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_messages.id", ondelete="CASCADE"), index=True
    )
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), index=True
    )
    folder: Mapped[str] = mapped_column(String(255), default="INBOX")
    uid_validity: Mapped[str] = mapped_column(String(64))
    uid: Mapped[int] = mapped_column(Integer)
    internal_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    message: Mapped[CanonicalMessage] = relationship(back_populates="occurrences")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_messages.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversion_status: Mapped[str] = mapped_column(String(32), default="pending")
    markdown_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_assets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    conversion_warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    converter_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    message: Mapped[CanonicalMessage] = relationship(back_populates="attachments")


class RuleSet(Base):
    __tablename__ = "rule_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), index=True
    )
    rule_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("rule_sets.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160), default="每日报告")
    cron_expression: Mapped[str] = mapped_column(String(120), default="0 9 * * 1-5")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    lookback_hours: Mapped[int] = mapped_column(Integer, default=24)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AIProviderProfile(Base):
    __tablename__ = "ai_provider_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    provider_type: Mapped[str] = mapped_column(String(64), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(1024))
    model_name: Mapped[str] = mapped_column(String(255))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ModelBinding(Base):
    __tablename__ = "model_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    mailbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="primary")
    provider_profile_id: Mapped[int] = mapped_column(
        ForeignKey("ai_provider_profiles.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), index=True
    )
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True
    )
    run_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    title: Mapped[str] = mapped_column(String(255), default="邮件归纳报告")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rendered_markdown: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_trace: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="smtp")
    destination: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    mailbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True
    )
    run_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="sync")
    status: Mapped[str] = mapped_column(String(32), default="running")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(120))
    target_type: Mapped[str] = mapped_column(String(64), default="")
    target_id: Mapped[str] = mapped_column(String(128), default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
