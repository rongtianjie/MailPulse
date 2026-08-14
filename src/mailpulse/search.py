from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import CanonicalMessage


class SearchService:
    table_name = "message_search"

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def ensure_index(engine: Engine) -> bool:
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS message_search "
                        "USING fts5(message_id UNINDEXED, owner_user_id UNINDEXED, "
                        "subject, sender, body_text)"
                    )
                )
            return True
        except Exception:
            return False

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
        self, owner_user_id: int, query: str = "", limit: int = 100
    ) -> list[CanonicalMessage]:
        query = query.strip()
        if query and self._fts_available() and len(query) >= 3:
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
                ids = [int(row[0]) for row in rows]
                if ids:
                    return list(
                        self.session.query(CanonicalMessage)
                        .filter(
                            CanonicalMessage.owner_user_id == owner_user_id,
                            CanonicalMessage.id.in_(ids),
                        )
                        .limit(limit)
                    )
            except Exception:
                # FTS syntax varies by SQLite build; always retain a safe LIKE fallback.
                pass
        statement = self.session.query(CanonicalMessage).filter(
            CanonicalMessage.owner_user_id == owner_user_id
        )
        if query:
            pattern = f"%{query}%"
            statement = statement.filter(
                (CanonicalMessage.subject.ilike(pattern))
                | (CanonicalMessage.sender.ilike(pattern))
                | (CanonicalMessage.body_text.ilike(pattern))
            )
        return list(statement.order_by(CanonicalMessage.received_at.desc()).limit(limit))

    def _fts_available(self) -> bool:
        try:
            with self.session.begin_nested():
                self.session.execute(text("SELECT count(*) FROM message_search")).scalar_one()
            return True
        except Exception:
            return False
