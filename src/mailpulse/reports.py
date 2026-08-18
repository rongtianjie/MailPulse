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
        "## 覆盖范围",
        (
            f"纳入 {summary.coverage.summarized_message_count}/"
            f"{summary.coverage.input_message_count} 封邮件"
            f"（模式：{summary.coverage.mode}）"
        ),
    ]
    if summary.coverage.omitted_message_ids:
        lines.append(
            "- 未形成事实卡片的邮件："
            + "、".join(str(value) for value in summary.coverage.omitted_message_ids)
        )
    if summary.coverage.truncated_message_ids:
        lines.append(
            "- 输入被截断的邮件："
            + "、".join(str(value) for value in summary.coverage.truncated_message_ids)
        )
    if summary.coverage.warnings:
        lines.extend([f"- {value}" for value in summary.coverage.warnings])
    if summary.message_summaries:
        lines.extend(["", "## 逐封邮件摘要"])
        for item in summary.message_summaries:
            title = f"邮件 {item.message_id}"
            if item.subject:
                title += f"：{item.subject}"
            lines.extend([f"### {title}", item.summary or "暂无摘要。"])
            if item.key_points:
                lines.extend([f"- {value}" for value in item.key_points])
    lines.extend([
        "",
        "## 行动项",
    ])
    if summary.action_items:
        for item in summary.action_items:
            owner = f"（负责人：{item.owner}）" if item.owner else ""
            due = f"，截止：{item.due_at}" if item.due_at else ""
            verified = "已引用来源" if item.verified else "未验证"
            refs = f"，来源：{'、'.join(item.source_refs)}" if item.source_refs else ""
            lines.append(f"- [ ] {item.action}{owner}{due}（{verified}{refs}）")
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
    if summary.source_refs:
        lines.extend(["", "## 来源引用"])
        for reference in summary.source_refs:
            location = [f"邮件 {reference.message_id}"]
            if reference.attachment_id is not None:
                location.append(f"附件 {reference.attachment_id}")
            if reference.page_number is not None:
                location.append(f"第 {reference.page_number} 页")
            if reference.image_index is not None:
                location.append(f"图片 {reference.image_index}")
            if reference.quote:
                location.append(f"摘录：{reference.quote}")
            lines.append("- " + "，".join(location))
    return "\n".join(lines)
