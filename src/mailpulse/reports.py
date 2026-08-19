from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bleach
import mistune
from markupsafe import Markup

from .ai.types import StructuredSummary

_MARKDOWN = mistune.create_markdown(
    escape=True,
    plugins=["strikethrough", "table", "task_lists"],
)
_ALLOWED_HTML_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "input",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_ALLOWED_HTML_ATTRIBUTES = {
    "a": ["href", "title"],
    "code": ["class"],
    "input": ["checked", "class", "disabled", "type"],
    "li": ["class"],
}
_KEYWORD_FIELDS = {
    "subject",
    "sender",
    "recipients",
    "cc",
    "body_text",
    "thread_key",
    "local_labels",
    "attachment_name",
    "attachment_type",
}
_KEYWORD_OPERATORS = {"contains", "equals", "starts_with", "ends_with", "in"}
_MAX_TITLE_KEYWORDS = 5
_MAX_KEYWORD_LENGTH = 24


def render_markdown_html(markdown_text: str) -> Markup:
    """Render untrusted Markdown to allowlisted HTML safe for templates and email."""
    rendered = _MARKDOWN(markdown_text or "")
    sanitized = bleach.clean(
        rendered,
        tags=_ALLOWED_HTML_TAGS,
        attributes=_ALLOWED_HTML_ATTRIBUTES,
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    return Markup(sanitized)


def render_markdown_email_html(markdown_text: str) -> str:
    """Build a self-contained HTML alternative while retaining a plain-text MIME part."""
    content = render_markdown_html(markdown_text)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ margin: 0; padding: 24px; color: #202636; background: #f6f7fb;
      font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .report {{ max-width: 760px; margin: 0 auto; padding: 28px; background: #fff;
      border: 1px solid #e2e6ef; border-radius: 12px; }}
    h1, h2, h3 {{ line-height: 1.35; color: #1d2945; }}
    h1 {{ font-size: 24px; }} h2 {{ margin-top: 28px; font-size: 19px; }}
    h3 {{ margin-top: 22px; font-size: 16px; }}
    a {{ color: #3157b7; }} blockquote {{ margin-left: 0; padding-left: 14px;
      color: #59647a; border-left: 3px solid #cbd4ea; }}
    pre {{ overflow-x: auto; padding: 14px; background: #f4f6fa; border-radius: 8px; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px 10px; text-align: left; border: 1px solid #dfe4ee; }}
    input[type="checkbox"] {{ margin-right: 7px; }}
  </style>
</head>
<body><main class="report">{content}</main></body>
</html>"""


def render_report_delivery_markdown(
    rendered_markdown: str,
    action_count: int,
    completed_action_indices: set[int],
) -> str:
    """Overlay current action state on a report snapshot without mutating the report."""
    if action_count <= 0:
        return rendered_markdown
    heading = re.search(r"(?m)^## 行动项\s*$", rendered_markdown)
    if heading is None:
        return rendered_markdown
    section_start = heading.end()
    next_section = re.search(r"(?m)^## ", rendered_markdown[section_start:])
    section_end = (
        section_start + next_section.start() if next_section is not None else len(rendered_markdown)
    )
    action_index = 0

    def replace_checkbox(match: re.Match[str]) -> str:
        nonlocal action_index
        if action_index >= action_count:
            return match.group(0)
        marker = "x" if action_index in completed_action_indices else " "
        action_index += 1
        return f"{match.group(1)}[{marker}]"

    section = re.sub(
        r"(?m)^(\s*[-*+]\s*)\[[ xX]\]",
        replace_checkbox,
        rendered_markdown[section_start:section_end],
    )
    return rendered_markdown[:section_start] + section + rendered_markdown[section_end:]


def extract_filter_keywords(rule_sets: Iterable[Any]) -> list[str]:
    """Extract concise positive text values from enabled rule definitions."""
    keywords: list[str] = []
    seen: set[str] = set()

    def append(value: Any) -> None:
        text = " ".join(str(value).split()).replace("｜", " ").strip("、,，;；")
        if not text:
            return
        text = text[:_MAX_KEYWORD_LENGTH]
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        keywords.append(text)

    def visit(node: Mapping[str, Any], negated: bool = False) -> None:
        kind = node.get("kind")
        if kind == "group":
            is_not = node.get("operator") == "not"
            for child in node.get("children", []):
                if isinstance(child, Mapping):
                    visit(child, negated or is_not)
            return
        if (
            kind != "condition"
            or negated
            or node.get("field") not in _KEYWORD_FIELDS
            or node.get("operator") not in _KEYWORD_OPERATORS
        ):
            return
        value = node.get("value")
        values = value if isinstance(value, list) else [value]
        for item in values:
            append(item)

    for rule_set in rule_sets:
        if not getattr(rule_set, "is_enabled", False):
            continue
        definition = getattr(rule_set, "definition", None)
        if isinstance(definition, Mapping):
            visit(definition)
        if len(keywords) >= _MAX_TITLE_KEYWORDS:
            break
    return keywords[:_MAX_TITLE_KEYWORDS]


def build_report_title(
    period_start: datetime,
    period_end: datetime,
    timezone: str,
    keywords: Iterable[str] = (),
) -> str:
    """Build a bounded report and email title from the effective filter window."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")

    def localize(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(zone)

    start = localize(period_start)
    end = localize(period_end)
    if start.year == end.year:
        time_range = f"{start:%m-%d %H:%M}–{end:%m-%d %H:%M}"
    else:
        time_range = f"{start:%Y-%m-%d %H:%M}–{end:%Y-%m-%d %H:%M}"
    title = f"邮件归纳｜{time_range}"
    keyword_text = "、".join(keywords)
    if keyword_text:
        title += f"｜关键词：{keyword_text}"
    return title[:255]


def render_summary_markdown(
    summary: StructuredSummary,
    period_start: datetime,
    period_end: datetime,
    title: str | None = None,
) -> str:
    default_title = (
        f"邮件归纳报告（{period_start:%Y-%m-%d %H:%M} 至 "
        f"{period_end:%Y-%m-%d %H:%M}）"
    )
    lines = [
        f"# {title or default_title}",
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
