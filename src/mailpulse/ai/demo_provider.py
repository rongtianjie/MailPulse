from __future__ import annotations

import json

from .types import GenerationRequest, GenerationResult


class DemoProvider:
    """Deterministic provider used by the demo flow and automated tests."""

    def __init__(self, name: str = "demo-model"):
        self.name = name

    def test_connection(self) -> None:
        return None

    def get_model_name(self) -> str:
        return self.name

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.role == "vision_extractor":
            parsed = {"evidence": []}
        elif request.role == "message_extractor":
            items = []
            seen_ids: set[int] = set()
            for part in request.content_parts:
                text = getattr(part, "text", "")
                for line in text.splitlines():
                    if not line.startswith("{"):
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message_id = record.get("message_id")
                    if not str(message_id).isdigit() or int(message_id) in seen_ids:
                        continue
                    message_id = int(message_id)
                    seen_ids.add(message_id)
                    items.append(
                        {
                            "message_id": message_id,
                            "thread_key": record.get("thread_key"),
                            "subject": record.get("subject") or "",
                            "received_at": record.get("received_at"),
                            "summary": f"演示事实卡片：{record.get('subject') or '无主题'}",
                            "source_refs": [{"message_id": message_id}],
                        }
                    )
            parsed = {"items": items}
        else:
            parsed = {
                "category": "项目与财务",
                "priority": "high",
                "summary": "演示报告：邮件中包含项目排期、风险整理和付款截止日期等信息。",
                "action_items": [
                    {
                        "action": "确认测试排期并整理风险清单",
                        "owner": "王工",
                        "due_at": "本周五",
                        "source_refs": ["demo"],
                        "verified": False,
                    }
                ],
                "decisions": [],
                "risks": ["付款截止日期需要在系统中进一步确认来源邮件。"],
                "questions": [],
                "source_refs": [],
                "attachment_status": ["附件已通过 MarkItDown 转换为 Markdown。"],
            }
        return GenerationResult(text=str(parsed), parsed_json=parsed, model_name=self.name)
