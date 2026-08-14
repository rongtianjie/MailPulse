from __future__ import annotations

from datetime import datetime

from .ai.types import StructuredSummary


def render_summary_markdown(
    summary: StructuredSummary, period_start: datetime, period_end: datetime
) -> str:
    lines = [
        f"# 邮件归纳报告（{period_start:%Y-%m-%d %H:%M} 至 {period_end:%Y-%m-%d %H:%M}）",
        "",
        f"**分类**：{summary.category}  ",
        f"**优先级**：{summary.priority}",
        "",
        "## 摘要",
        summary.summary or "暂无摘要。",
        "",
        "## 行动项",
    ]
    if summary.action_items:
        for item in summary.action_items:
            owner = f"（负责人：{item.owner}）" if item.owner else ""
            due = f"，截止：{item.due_at}" if item.due_at else ""
            verified = "已引用来源" if item.verified else "未验证"
            lines.append(f"- [ ] {item.action}{owner}{due}（{verified}）")
    else:
        lines.append("- 无")
    for title, values in (
        ("决策", summary.decisions),
        ("风险", summary.risks),
        ("待确认问题", summary.questions),
    ):
        lines.extend(["", f"## {title}"])
        lines.extend([f"- {value}" for value in values] or ["- 无"])
    if summary.attachment_status:
        lines.extend(
            ["", "## 附件处理状态", *[f"- {value}" for value in summary.attachment_status]]
        )
    return "\n".join(lines)
