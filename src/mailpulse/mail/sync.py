from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Attachment, CanonicalMessage, Mailbox, MessageOccurrence
from ..search import SearchService
from .connectors import MailConnector
from .types import RawMessage, SyncBatch, SyncCursor


def normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", "", value).strip().lower()


def message_content_hash(message: RawMessage) -> str:
    parts = [
        message.subject.strip().lower(),
        message.sender.strip().lower(),
        "\n".join(sorted(address.lower() for address in message.recipients)),
        "\n".join(sorted(address.lower() for address in message.cc)),
        message.received_at.isoformat() if message.received_at else "",
        message.body_text.strip(),
        "\n".join(
            f"{item.filename}|{item.mime_type}|{len(item.payload)}|"
            f"{hashlib.sha256(item.payload).hexdigest()}"
            for item in message.attachments
        ),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8", errors="replace")).hexdigest()


@dataclass(slots=True)
class SyncResult:
    fetched: int = 0
    created: int = 0
    linked: int = 0
    attachments: int = 0
    cursor: SyncCursor | None = None


class MailSyncService:
    def __init__(self, session: Session, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    def sync(self, mailbox: Mailbox, connector: MailConnector) -> SyncResult:
        cursor = None
        if mailbox.sync_uid_validity:
            cursor = SyncCursor(mailbox.sync_uid_validity, mailbox.sync_last_uid)
        batch = connector.sync_messages(cursor)
        result = SyncResult(fetched=len(batch.messages), cursor=batch.cursor)
        storage_state = self._storage_state(mailbox.user_id)
        existing = self._prefetch_existing(mailbox, batch)
        for uid, raw_message in batch.messages:
            created = self._store_message(
                mailbox, uid, batch.cursor.uid_validity, raw_message, storage_state, existing
            )
            result.created += int(created)
            result.linked += int(not created)
            result.attachments += len(raw_message.attachments)
        mailbox.sync_uid_validity = batch.cursor.uid_validity
        mailbox.sync_last_uid = batch.cursor.last_uid
        mailbox.last_synced_at = datetime.now(UTC)
        mailbox.sync_error = None
        self.session.flush()
        result.cursor = batch.cursor
        return result

    def _prefetch_existing(self, mailbox: Mailbox, batch: SyncBatch) -> dict[str, Any]:
        """Load dedup lookups for the whole batch in three queries instead of N+1."""
        uids = [uid for uid, _ in batch.messages]
        existing_uids: set[int] = set()
        if uids:
            rows = self.session.execute(
                select(MessageOccurrence.uid).where(
                    MessageOccurrence.mailbox_id == mailbox.id,
                    MessageOccurrence.folder == mailbox.folder,
                    MessageOccurrence.uid_validity == batch.cursor.uid_validity,
                    MessageOccurrence.uid.in_(uids),
                )
            ).all()
            existing_uids = {row[0] for row in rows}
        normalized_ids = {
            normalize_message_id(raw.message_id) for _, raw in batch.messages
        }
        normalized_ids.discard(None)
        by_message_id: dict[str, CanonicalMessage] = {}
        if normalized_ids:
            for message in self.session.scalars(
                select(CanonicalMessage).where(
                    CanonicalMessage.owner_user_id == mailbox.user_id,
                    CanonicalMessage.message_id.in_(normalized_ids),
                )
            ):
                by_message_id.setdefault(str(message.message_id), message)
        hashes = {message_content_hash(raw) for _, raw in batch.messages}
        by_content_hash: dict[str, CanonicalMessage] = {}
        if hashes:
            for message in self.session.scalars(
                select(CanonicalMessage).where(
                    CanonicalMessage.owner_user_id == mailbox.user_id,
                    CanonicalMessage.content_hash.in_(hashes),
                )
            ):
                by_content_hash.setdefault(message.content_hash, message)
        return {
            "existing_uids": existing_uids,
            "by_message_id": by_message_id,
            "by_content_hash": by_content_hash,
        }

    def _store_message(
        self,
        mailbox: Mailbox,
        uid: int,
        uid_validity: str,
        raw: RawMessage,
        storage_state: dict[str, int],
        existing: dict[str, Any],
    ) -> bool:
        normalized_id = normalize_message_id(raw.message_id)
        content_hash = message_content_hash(raw)
        received_at = _as_utc(raw.received_at)
        internal_date = _as_utc(raw.internal_date or raw.received_at)
        if uid in existing["existing_uids"]:
            return False

        canonical = existing["by_message_id"].get(normalized_id) if normalized_id else None
        if canonical is None:
            canonical = existing["by_content_hash"].get(content_hash)
        is_new = canonical is None
        if canonical is None:
            canonical = CanonicalMessage(
                owner_user_id=mailbox.user_id,
                message_id=normalized_id,
                content_hash=content_hash,
                subject=raw.subject,
                sender=raw.sender,
                recipients=raw.recipients,
                cc=raw.cc,
                received_at=received_at,
                body_text=raw.body_text,
                thread_key=raw.thread_key,
            )
            self.session.add(canonical)
            self.session.flush()
            SearchService(self.session).index_message(canonical)
        # Keep the in-memory lookup cache current so duplicate messages in the
        # same IMAP batch share this canonical row instead of creating another.
        if normalized_id:
            existing["by_message_id"][normalized_id] = canonical
        existing["by_content_hash"][content_hash] = canonical
        occurrence = MessageOccurrence(
            message_id=canonical.id,
            mailbox_id=mailbox.id,
            folder=mailbox.folder,
            uid_validity=uid_validity,
            uid=uid,
            source_id=mailbox.sync_source_id,
            internal_date=internal_date,
        )
        self.session.add(occurrence)
        existing["existing_uids"].add(uid)
        if is_new:
            self._store_attachments(canonical, raw, storage_state)
        return is_new

    def _store_attachments(
        self,
        canonical: CanonicalMessage,
        raw: RawMessage,
        storage_state: dict[str, int],
    ) -> None:
        for index, item in enumerate(raw.attachments):
            digest = hashlib.sha256(item.payload).hexdigest()
            size_bytes = len(item.payload)
            if index >= self.settings.max_attachments_per_message:
                self.session.add(
                    Attachment(
                        message_id=canonical.id,
                        filename=item.filename,
                        mime_type=item.mime_type,
                        size_bytes=size_bytes,
                        content_hash=digest,
                        conversion_status="too_many",
                        conversion_warnings=["邮件附件数量超过限制"],
                    )
                )
                continue
            if size_bytes > self.settings.max_attachment_bytes:
                self.session.add(
                    Attachment(
                        message_id=canonical.id,
                        filename=item.filename,
                        mime_type=item.mime_type,
                        size_bytes=size_bytes,
                        content_hash=digest,
                        conversion_status="too_large",
                        conversion_warnings=["附件超过单文件大小限制"],
                    )
                )
                continue
            if (
                storage_state["user"] + size_bytes > self.settings.max_user_storage_bytes
                or storage_state["global"] + size_bytes > self.settings.max_global_storage_bytes
            ):
                self.session.add(
                    Attachment(
                        message_id=canonical.id,
                        filename=item.filename,
                        mime_type=item.mime_type,
                        size_bytes=size_bytes,
                        content_hash=digest,
                        conversion_status="storage_limit",
                        conversion_warnings=["附件存储空间达到用户或全局上限"],
                    )
                )
                continue
            safe_name = _safe_filename(item.filename)
            relative = Path(str(canonical.owner_user_id)) / digest[:2] / f"{digest}-{safe_name}"
            target = self.settings.attachments_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix=f".{target.name}.", delete=False
                ) as temporary:
                    temporary.write(item.payload)
                    temporary.flush()
                    temporary_path = Path(temporary.name)
                temporary_path.replace(target)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            self.session.add(
                Attachment(
                    message_id=canonical.id,
                    filename=safe_name,
                    mime_type=item.mime_type,
                    size_bytes=size_bytes,
                    content_hash=digest,
                    storage_path=str(target),
                    conversion_status="pending",
                )
            )
            storage_state["user"] += size_bytes
            storage_state["global"] += size_bytes

    def _storage_state(self, user_id: int) -> dict[str, int]:
        """User and global attachment quotas in a single aggregate query."""
        row = self.session.execute(
            select(
                func.coalesce(func.sum(Attachment.size_bytes), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (CanonicalMessage.owner_user_id == user_id, Attachment.size_bytes),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .select_from(Attachment)
            .join(CanonicalMessage, CanonicalMessage.id == Attachment.message_id)
            .where(Attachment.storage_path.is_not(None))
        ).one()
        return {"user": int(row[1]), "global": int(row[0])}


def _safe_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "attachment.bin")[:180]


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
