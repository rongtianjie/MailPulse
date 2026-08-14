from __future__ import annotations

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
