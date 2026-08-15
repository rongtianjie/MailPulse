from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any


class RuleValidationError(ValueError):
    pass


class RuleEvaluator:
    """Evaluate a small JSON rule tree without eval or arbitrary code execution."""

    allowed_fields = {
        "subject",
        "sender",
        "recipients",
        "cc",
        "body_text",
        "received_at",
        "thread_key",
        "local_labels",
        "local_starred",
        "local_processed",
        "attachment_name",
        "attachment_type",
        "attachment_size",
    }
    allowed_operators = {
        "equals",
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
        "regex",
        "greater_than",
        "less_than",
        "in",
        "exists",
    }

    def validate(self, node: Mapping[str, Any]) -> None:
        kind = node.get("kind")
        if kind in {"match_all", "match_none"}:
            return
        if kind == "group":
            operator = node.get("operator")
            if operator not in {"and", "or", "not"}:
                raise RuleValidationError("规则组 operator 必须是 and、or 或 not")
            children = node.get("children")
            if not isinstance(children, list) or not children:
                raise RuleValidationError("规则组必须包含 children")
            if operator == "not" and len(children) != 1:
                raise RuleValidationError("not 规则组只能包含一个 child")
            for child in children:
                if not isinstance(child, Mapping):
                    raise RuleValidationError("规则 child 必须是对象")
                self.validate(child)
            return
        if kind == "condition":
            field = node.get("field")
            operator = node.get("operator")
            if field not in self.allowed_fields:
                raise RuleValidationError(f"不支持的字段: {field}")
            if operator not in self.allowed_operators:
                raise RuleValidationError(f"不支持的操作符: {operator}")
            if operator == "regex" and (
                not isinstance(node.get("value"), str) or len(node["value"]) > 256
            ):
                raise RuleValidationError("正则表达式必须是 256 字符以内的字符串")
            return
        raise RuleValidationError("规则节点 kind 必须是 group 或 condition")

    def evaluate(self, node: Mapping[str, Any], message: Any) -> bool:
        self.validate(node)
        values = asdict(message) if is_dataclass(message) else message
        if node["kind"] == "match_all":
            return True
        if node["kind"] == "match_none":
            return False
        if node["kind"] == "group":
            children = node["children"]
            if node["operator"] == "and":
                return all(self.evaluate(child, values) for child in children)
            if node["operator"] == "or":
                return any(self.evaluate(child, values) for child in children)
            return not self.evaluate(children[0], values)

        actual = self._field_value(values, node["field"])
        expected = node.get("value")
        operator = node["operator"]
        if operator == "exists":
            return bool(actual) is bool(expected if expected is not None else True)
        if isinstance(actual, list):
            actual_values = [str(item).lower() for item in actual]
            actual_text = " ".join(actual_values)
        else:
            actual_values = [str(actual).lower()] if actual is not None else []
            actual_text = actual_values[0] if actual_values else ""
        expected_text = str(expected).lower() if expected is not None else ""
        if operator == "equals":
            return actual_text == expected_text
        if operator == "contains":
            return expected_text in actual_text
        if operator == "not_contains":
            return expected_text not in actual_text
        if operator == "starts_with":
            return actual_text.startswith(expected_text)
        if operator == "ends_with":
            return actual_text.endswith(expected_text)
        if operator == "regex":
            try:
                return re.search(str(expected), actual_text, flags=re.IGNORECASE) is not None
            except re.error as exc:
                raise RuleValidationError(f"非法正则表达式: {exc}") from exc
        if operator == "in":
            if not isinstance(expected, list):
                raise RuleValidationError("in 操作符的 value 必须是数组")
            choices = {str(item).lower() for item in expected}
            return any(value in choices for value in actual_values)
        if operator in {"greater_than", "less_than"}:
            actual_items = actual if isinstance(actual, list) else [actual]
            for item in actual_items:
                try:
                    left = float(item)
                    right = float(expected)
                except (TypeError, ValueError) as exc:
                    if isinstance(item, datetime) and isinstance(expected, str):
                        left = item.timestamp()
                        right = datetime.fromisoformat(expected).timestamp()
                    else:
                        raise RuleValidationError("比较操作的值必须可转换为数字或日期") from exc
                if left > right if operator == "greater_than" else left < right:
                    return True
            return False
        return False

    @staticmethod
    def _field_value(values: Mapping[str, Any], field: str) -> Any:
        if field.startswith("attachment_"):
            attachments = values.get("attachments", [])
            if field == "attachment_name":
                return [getattr(item, "filename", "") for item in attachments]
            if field == "attachment_type":
                return [getattr(item, "mime_type", "") for item in attachments]
            if field == "attachment_size":
                return [
                    getattr(item, "size_bytes", None)
                    if getattr(item, "size_bytes", None) is not None
                    else len(getattr(item, "payload", b""))
                    for item in attachments
                ]
        return values.get(field)
