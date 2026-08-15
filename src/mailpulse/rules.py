from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .filtering import RuleEvaluator
from .models import Attachment, CanonicalMessage, RuleSet

MATCH_ALL = {"kind": "match_all"}


def message_rule_data(session: Session, message: CanonicalMessage) -> dict[str, Any]:
    attachments = list(
        session.scalars(select(Attachment).where(Attachment.message_id == message.id))
    )
    return {
        "subject": message.subject,
        "sender": message.sender,
        "recipients": message.recipients,
        "cc": message.cc,
        "body_text": message.body_text,
        "received_at": message.received_at,
        "thread_key": message.thread_key,
        "local_labels": message.local_labels,
        "local_starred": message.local_starred,
        "local_processed": message.local_processed,
        "attachments": attachments,
    }


class RuleService:
    def __init__(self, session: Session):
        self.session = session
        self.evaluator = RuleEvaluator()

    def validate(self, definition: dict[str, Any]) -> None:
        self.evaluator.validate(definition)

    def filter_messages(
        self,
        messages: Iterable[CanonicalMessage],
        rule_set: RuleSet | None,
    ) -> list[CanonicalMessage]:
        if rule_set is None:
            return list(messages)
        definition = rule_set.definition or MATCH_ALL
        self.validate(definition)
        return [
            message
            for message in messages
            if self.evaluator.evaluate(definition, message_rule_data(self.session, message))
        ]

    def filter_messages_any(
        self,
        messages: Iterable[CanonicalMessage],
        rule_sets: Iterable[RuleSet],
    ) -> list[CanonicalMessage]:
        """Keep messages matched by any enabled rule set (OR union).

        A message is included when at least one enabled rule set matches it;
        duplicates are removed while preserving the input order. With no
        enabled rule sets every message is kept.
        """
        enabled = [item for item in rule_sets if item is not None and item.is_enabled]
        if not enabled:
            return list(messages)
        result: list[CanonicalMessage] = []
        seen: set[int] = set()
        for rule_set in enabled:
            for message in self.filter_messages(messages, rule_set):
                if message.id not in seen:
                    seen.add(message.id)
                    result.append(message)
        return result
