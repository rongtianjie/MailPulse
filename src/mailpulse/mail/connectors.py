from __future__ import annotations

import email
import imaplib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Protocol

from .types import MailboxConnection, RawAttachment, RawMessage, SyncBatch, SyncCursor


class MailConnector(Protocol):
    def test_connection(self) -> None: ...

    def sync_messages(self, cursor: SyncCursor | None = None) -> SyncBatch: ...


_IMAP_TIMEOUT_SECONDS = 30
_FETCH_CHUNK_SIZE = 200


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _fetch_uid(header: bytes) -> int | None:
    match = re.search(rb"UID\s+(\d+)", header)
    return int(match.group(1)) if match else None


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, ValueError):
        return value


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _parse_message(raw: bytes) -> RawMessage:
    message = email.message_from_bytes(raw)
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[RawAttachment] = []

    parts: Iterable[Message] = message.walk() if message.is_multipart() else [message]
    for part in parts:
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                RawAttachment(
                    filename=_decode_header(filename) or "attachment.bin",
                    mime_type=part.get_content_type(),
                    payload=payload,
                )
            )
            continue
        if part.get_content_type() == "text/plain":
            text_parts.append(_decode_payload(part))
        elif part.get_content_type() == "text/html":
            html_parts.append(_strip_html(_decode_payload(part)))

    body_text = "\n\n".join(text_parts).strip()
    if not body_text:
        body_text = "\n\n".join(html_parts).strip()
    received_at = None
    if message.get("Date"):
        try:
            received_at = parsedate_to_datetime(message["Date"])
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
        except (TypeError, ValueError, IndexError):
            received_at = None

    sender = getaddresses([message.get("From", "")])[0][1] if message.get("From") else ""
    recipients = [address for _, address in getaddresses(message.get_all("To", [])) if address]
    cc = [address for _, address in getaddresses(message.get_all("Cc", [])) if address]
    references = (
        message.get("References") or message.get("In-Reply-To") or message.get("Message-ID")
    )
    thread_key = " ".join(str(references).split()) if references else None

    return RawMessage(
        message_id=message.get("Message-ID"),
        subject=_decode_header(message.get("Subject")),
        sender=sender,
        recipients=recipients,
        cc=cc,
        received_at=received_at,
        body_text=body_text,
        thread_key=thread_key,
        attachments=attachments,
    )


class IMAPConnector:
    def __init__(self, connection: MailboxConnection):
        self.connection = connection

    def _open(self):
        if self.connection.tls:
            client = imaplib.IMAP4_SSL(
                self.connection.host, self.connection.port, timeout=_IMAP_TIMEOUT_SECONDS
            )
        else:
            client = imaplib.IMAP4(
                self.connection.host, self.connection.port, timeout=_IMAP_TIMEOUT_SECONDS
            )
        client.login(self.connection.username, self.connection.password)
        status, _ = client.select(self.connection.folder, readonly=True)
        if status != "OK":
            client.logout()
            raise ConnectionError(f"无法选择邮箱文件夹: {self.connection.folder}")
        return client

    def test_connection(self) -> None:
        client = self._open()
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass

    def sync_messages(self, cursor: SyncCursor | None = None) -> SyncBatch:
        client = self._open()
        try:
            uid_validity = client.response("UIDVALIDITY")[1]
            raw_validity = uid_validity[0] if uid_validity else b"0"
            current_validity = raw_validity.decode(errors="replace")
            reset = cursor is None or cursor.uid_validity != current_validity
            last_uid = 0 if reset else cursor.last_uid
            status, data = client.uid("SEARCH", None, "ALL")
            if status != "OK":
                raise ConnectionError("IMAP SEARCH 失败")
            uids = [int(value) for value in (data[0] or b"").split() if int(value) > last_uid]
            messages: list[tuple[int, RawMessage]] = []
            # Batch FETCH dramatically reduces round trips on first full sync.
            for chunk in _chunks(uids, _FETCH_CHUNK_SIZE):
                status, fetched = client.uid(
                    "FETCH",
                    ",".join(str(uid) for uid in chunk),
                    "(UID BODY.PEEK[])",
                )
                if status != "OK":
                    continue
                # imaplib returns a trailing non-tuple marker such as b")" for
                # BODY fetches. Only tuple entries carry a message payload.
                for item in fetched:
                    if not isinstance(item, tuple) or len(item) != 2:
                        continue
                    header, payload = item
                    if not isinstance(header, bytes) or not isinstance(payload, bytes):
                        continue
                    uid = _fetch_uid(header)
                    if uid is None or uid <= last_uid:
                        continue
                    messages.append((uid, _parse_message(payload)))
            messages.sort(key=lambda item: item[0])
            return SyncBatch(
                cursor=SyncCursor(
                    uid_validity=current_validity,
                    last_uid=max([last_uid, *uids], default=last_uid),
                ),
                messages=messages,
            )
        finally:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass


@dataclass
class FakeMailConnector:
    messages: list[RawMessage]
    uid_validity: str = "fake-1"

    def test_connection(self) -> None:
        return None

    def sync_messages(self, cursor: SyncCursor | None = None) -> SyncBatch:
        last_uid = (
            0 if cursor is None or cursor.uid_validity != self.uid_validity else cursor.last_uid
        )
        batch = [
            (index, message)
            for index, message in enumerate(self.messages, start=1)
            if index > last_uid
        ]
        return SyncBatch(
            cursor=SyncCursor(uid_validity=self.uid_validity, last_uid=len(self.messages)),
            messages=batch,
        )
