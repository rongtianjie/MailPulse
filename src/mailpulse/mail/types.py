from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class RawAttachment:
    filename: str
    mime_type: str
    payload: bytes


@dataclass(slots=True)
class RawMessage:
    message_id: str | None
    subject: str
    sender: str
    recipients: list[str]
    cc: list[str]
    received_at: datetime | None
    body_text: str
    thread_key: str | None
    attachments: list[RawAttachment] = field(default_factory=list)


@dataclass(slots=True)
class SyncCursor:
    uid_validity: str
    last_uid: int


@dataclass(slots=True)
class SyncBatch:
    cursor: SyncCursor
    messages: list[tuple[int, RawMessage]]


@dataclass(slots=True)
class MailboxConnection:
    host: str
    port: int
    username: str
    password: str
    tls: bool = True
    folder: str = "INBOX"
