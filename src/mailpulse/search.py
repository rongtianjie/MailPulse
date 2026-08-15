from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import CanonicalMessage


class SearchService:
    table_name = "message_search"
    # trigram tokenizer enables CJK substring matching; unicode61 treats a run of
    # CJK characters as one token, which silently breaks Chinese search.
    _ddl = (
        "CREATE VIRTUAL TABLE message_search USING fts5("
        "message_id UNINDEXED, owner_user_id UNINDEXED, "
        "subject, sender, body_text, tokenize = 'trigram')"
    )

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def ensure_index(engine: Engine) -> str:
        """Create or upgrade the FTS index; returns 'created', 'rebuilt' or 'unchanged'."""
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'message_search'"
                )
            ).first()
            if row is None:
                connection.execute(text(SearchService._ddl))
                return "created"
            if "tokenize" not in str(row[0]).lower():
                # Old unicode61 index cannot match CJK substrings; rebuild it.
                connection.execute(text("DROP TABLE message_search"))
                connection.execute(text(SearchService._ddl))
                return "rebuilt"
            return "unchanged"

    def reindex_all(self) -> int:
        """Bulk-populate the FTS index from canonical_messages; best effort."""
        try:
            with self.session.begin_nested():
                result = self.session.execute(
                    text(
                        "INSERT INTO message_search(message_id, owner_user_id, subject, sender, "
                        "body_text) SELECT id, owner_user_id, subject, sender, body_text "
                        "FROM canonical_messages"
                    )
                )
            return int(result.rowcount or 0)
        except Exception:
            return 0

    def index_message(self, message: CanonicalMessage) -> bool:
        """Best-effort FTS indexing; message persistence must not depend on FTS5."""
        try:
            with self.session.begin_nested():
                self.session.execute(
                    text("DELETE FROM message_search WHERE message_id = :message_id"),
                    {"message_id": str(message.id)},
                )
                self.session.execute(
                    text(
                        "INSERT INTO message_search(message_id, owner_user_id, subject, sender, "
                        "body_text) "
                        "VALUES (:message_id, :owner_user_id, :subject, :sender, :body_text)"
                    ),
                    {
                        "message_id": str(message.id),
                        "owner_user_id": str(message.owner_user_id),
                        "subject": message.subject,
                        "sender": message.sender,
                        "body_text": message.body_text,
                    },
                )
        except Exception:
            # FTS5 is optional. The normal field query remains available as a fallback.
            return False
        return True

    def search(
        self,
        owner_user_id: int,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
        status: str = "",
    ) -> list[CanonicalMessage]:
        query = query.strip()
        statement = self.session.query(CanonicalMessage).filter(
            CanonicalMessage.owner_user_id == owner_user_id
        )
        if query:
            use_fts = self._use_fts(query)
            fts_ids = self._fts_ids(owner_user_id, query, limit + offset) if use_fts else None
            if fts_ids is None:
                statement = self._apply_like(statement, query)
            elif not fts_ids:
                return []
            else:
                statement = statement.filter(CanonicalMessage.id.in_(fts_ids))
        statement = self._apply_status(statement, status)
        return list(
            statement.order_by(CanonicalMessage.received_at.desc()).offset(offset).limit(limit)
        )

    def count(self, owner_user_id: int, query: str = "", status: str = "") -> int:
        """Total matches for pagination; mirrors search() filtering semantics."""
        query = query.strip()
        if query and self._use_fts(query):
            try:
                with self.session.begin_nested():
                    total = self.session.execute(
                        text(
                            "SELECT count(*) FROM message_search "
                            "WHERE owner_user_id = :owner_id AND message_search MATCH :query"
                        ),
                        {"owner_id": str(owner_user_id), "query": query},
                    ).scalar_one()
                return int(total)
            except Exception:
                pass
        statement = (
            select(func.count())
            .select_from(CanonicalMessage)
            .where(CanonicalMessage.owner_user_id == owner_user_id)
        )
        if query:
            statement = self._apply_like(statement, query)
        statement = self._apply_status(statement, status)
        return int(self.session.scalar(statement) or 0)

    def _use_fts(self, query: str) -> bool:
        return len(query) >= 3 and self._fts_available()

    def _fts_ids(self, owner_user_id: int, query: str, limit: int) -> list[int] | None:
        try:
            with self.session.begin_nested():
                rows = self.session.execute(
                    text(
                        "SELECT message_id FROM message_search "
                        "WHERE owner_user_id = :owner_id AND message_search MATCH :query "
                        "LIMIT :limit"
                    ),
                    {"owner_id": str(owner_user_id), "query": query, "limit": limit},
                ).all()
            return [int(row[0]) for row in rows]
        except Exception:
            return None

    @staticmethod
    def _apply_like(statement, query: str):
        pattern = f"%{query}%"
        return statement.filter(
            (CanonicalMessage.subject.ilike(pattern))
            | (CanonicalMessage.sender.ilike(pattern))
            | (CanonicalMessage.body_text.ilike(pattern))
        )

    @staticmethod
    def _apply_status(statement, status: str):
        if status == "processed":
            return statement.filter(CanonicalMessage.local_processed.is_(True))
        if status == "unprocessed":
            return statement.filter(CanonicalMessage.local_processed.is_(False))
        if status == "starred":
            return statement.filter(CanonicalMessage.local_starred.is_(True))
        return statement

    def _fts_available(self) -> bool:
        try:
            with self.session.begin_nested():
                self.session.execute(text("SELECT count(*) FROM message_search")).scalar_one()
            return True
        except Exception:
            return False
