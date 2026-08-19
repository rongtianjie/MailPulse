from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai.demo_provider import DemoProvider
from .ai.orchestrator import AIOrchestrator, ModelRouter
from .ai.profile_service import AIProfileService
from .ai.providers import OpenAICompatibleProvider
from .ai.types import ModelCapabilities, ModelProfile
from .attachments.converter import MarkItDownAttachmentConverter
from .config import Settings, get_settings
from .mail.types import RawMessage
from .models import (
    Attachment,
    AuditLog,
    CanonicalMessage,
    Mailbox,
    MessageOccurrence,
    Report,
    RuleSet,
    Task,
    User,
)
from .reports import build_report_title, extract_filter_keywords, render_summary_markdown
from .rules import RuleService


class ReportService:
    def __init__(self, session: Session, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    def generate_for_user(
        self,
        user: User,
        task: Task,
        use_demo_provider: bool = False,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        run_key: str | None = None,
    ) -> Report:
        if task.user_id != user.id:
            raise ValueError("任务不存在或不属于当前用户")
        period_end = period_end or datetime.now(UTC)
        if run_key is not None:
            existing = self.session.scalar(
                select(Report).where(Report.run_key == run_key, Report.user_id == user.id)
            )
            if existing is not None:
                return existing
        mailbox = self.session.scalar(
            select(Mailbox).where(Mailbox.id == task.mailbox_id, Mailbox.user_id == user.id)
        )
        if mailbox is None:
            raise ValueError("指定邮箱不存在或不属于当前用户")
        occurrence_time = func.coalesce(
            MessageOccurrence.internal_date, CanonicalMessage.received_at
        )
        query = (
            select(CanonicalMessage, occurrence_time.label("occurrence_time"))
            .join(MessageOccurrence, MessageOccurrence.message_id == CanonicalMessage.id)
            .where(
                CanonicalMessage.owner_user_id == user.id,
                MessageOccurrence.mailbox_id == mailbox.id,
                MessageOccurrence.source_id == mailbox.sync_source_id,
            )
        )
        if period_start is not None:
            query = query.where(
                occurrence_time >= period_start,
                occurrence_time <= period_end,
            )
        message_times: dict[int, datetime | None] = {}
        all_messages: list[CanonicalMessage] = []
        seen_message_ids: set[int] = set()
        for message, source_time in self.session.execute(
            query.order_by(occurrence_time.desc())
        ):
            if message.id in seen_message_ids:
                continue
            seen_message_ids.add(message.id)
            all_messages.append(message)
            message_times[message.id] = source_time or message.received_at
        rule_sets = list(
            self.session.scalars(
                select(RuleSet)
                .where(RuleSet.task_id == task.id)
                .order_by(RuleSet.priority.asc(), RuleSet.id.asc())
            )
        )
        matched_messages = RuleService(self.session).filter_messages_any(all_messages, rule_sets)
        matched_messages.sort(
            key=lambda item: message_times.get(item.id)
            or item.received_at
            or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        total_matches = len(matched_messages)
        messages = matched_messages[: self.settings.max_messages_per_report]
        if not messages:
            raise ValueError("当前时间范围内没有符合规则的邮件")

        converted = []
        attachments_by_message: dict[int, list[Attachment]] = {
            message.id: [] for message in messages
        }
        if messages:
            for attachment in self.session.scalars(
                select(Attachment).where(
                    Attachment.message_id.in_([message.id for message in messages])
                )
            ):
                attachments_by_message.setdefault(attachment.message_id, []).append(attachment)
        attachment_ids_by_message: dict[int, list[int]] = {
            message_id: [attachment.id for attachment in values]
            for message_id, values in attachments_by_message.items()
        }
        converter = MarkItDownAttachmentConverter(self.settings)
        for message in messages:
            for attachment in attachments_by_message.get(message.id, []):
                result = converter.convert(self.session, attachment)
                converted.append((attachment.id, result))

        raw_messages = [
            RawMessage(
                message_id=str(message.id),
                subject=message.subject,
                sender=message.sender,
                recipients=message.recipients,
                cc=message.cc,
                received_at=message_times.get(message.id) or message.received_at,
                body_text=message.body_text,
                thread_key=message.thread_key,
                attachment_ids=attachment_ids_by_message.get(message.id, []),
            )
            for message in messages
        ]
        orchestrator = self._build_orchestrator(user, mailbox.id, use_demo_provider)
        summary, trace = orchestrator.summarize(
            raw_messages,
            converted,
            task_context={
                "task_name": task.name,
                "period_start": period_start.isoformat() if period_start else None,
                "period_end": period_end.isoformat(),
            },
        )
        end = period_end
        matched_start = min(
            (
                message_times.get(message.id) or message.received_at
                for message in messages
                if message_times.get(message.id) or message.received_at
            ),
            default=period_start or end - timedelta(days=1),
        )
        start = period_start or matched_start
        conversion_status = [
            f"附件 {attachment_id}: {result.status}"
            + (f"（{'；'.join(result.warnings)}）" if result.warnings else "")
            for attachment_id, result in converted
        ]
        if conversion_status:
            summary.attachment_status = list(
                dict.fromkeys([*conversion_status, *summary.attachment_status])
            )
        summary_payload = summary.model_dump(mode="json")
        summary_payload["message_count"] = len(messages)
        summary_payload["matched_message_count"] = total_matches
        summary_payload["message_limit"] = self.settings.max_messages_per_report
        summary_payload["truncated"] = total_matches > len(messages)
        keywords = extract_filter_keywords(rule_sets)
        summary_payload["filter_keywords"] = keywords
        summary_payload["filter_period_start"] = start.isoformat()
        summary_payload["filter_period_end"] = end.isoformat()
        report_title = build_report_title(start, end, task.timezone, keywords)
        rendered_markdown = render_summary_markdown(summary, start, end, title=report_title)
        if trace.get("vision_error"):
            summary_payload["vision_degraded"] = True
            rendered_markdown += (
                "\n\n> 处理说明：视觉副模型处理失败，本报告未使用或未验证图片视觉证据；"
                "主模型已继续根据可用邮件文本生成结果。"
            )
        if total_matches > len(messages):
            rendered_markdown += (
                "\n\n> 说明：规则命中邮件共 "
                f"{total_matches} 封，本报告按上限纳入最新命中邮件 {len(messages)} 封，结果已截断。"
            )
        report = Report(
            user_id=user.id,
            mailbox_id=mailbox.id,
            task_id=task.id,
            run_key=run_key or f"manual:{user.id}:{uuid4().hex}",
            period_start=start,
            period_end=end,
            status="success",
            title=report_title,
            summary=summary_payload,
            rendered_markdown=rendered_markdown,
            model_trace=trace,
        )
        self.session.add(report)
        self.session.flush()
        self.session.add(
            AuditLog(
                actor_user_id=user.id,
                action="ai_generate",
                target_type="report",
                target_id=str(report.id),
                metadata_json={
                    "message_count": len(messages),
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "primary_model": trace.get("primary_model", "demo"),
                    "vision_model": trace.get("vision_model"),
                    "used_vision": trace.get("used_vision", False),
                },
            )
        )
        return report

    def _build_orchestrator(
        self, user: User, mailbox_id: int, use_demo_provider: bool
    ) -> AIOrchestrator:
        if use_demo_provider:
            primary = DemoProvider()
            return AIOrchestrator(
                ModelRouter(primary, primary_image_input=False),
                max_output_tokens=1800,
                max_input_chars=self.settings.ai_max_input_chars,
                retries=self.settings.ai_max_retries,
                message_batch_size=self.settings.ai_message_batch_size,
            )
        resolved = AIProfileService(self.session, self.settings).resolve_for(user.id, mailbox_id)
        primary = resolved.primary or self._build_environment_provider("primary")
        vision = resolved.vision
        if vision is None:
            vision = self._build_environment_provider("vision")
        if primary is not None:
            return AIOrchestrator(
                ModelRouter(
                    primary,
                    primary_image_input=(
                        resolved.primary_image_input
                        if resolved.primary
                        else self.settings.ai_primary_supports_image
                    ),
                    vision=vision,
                ),
                max_output_tokens=self.settings.ai_max_output_tokens,
                timeout=self.settings.ai_timeout_seconds,
                max_input_chars=self.settings.ai_max_input_chars,
                retries=self.settings.ai_max_retries,
                message_batch_size=self.settings.ai_message_batch_size,
            )
        raise RuntimeError("尚未配置 AI 主模型，请在管理控制台配置或设置 MAILPULSE_AI_BASE_URL")

    def _build_environment_provider(self, role: str) -> OpenAICompatibleProvider | None:
        if role == "primary":
            base_url = self.settings.ai_base_url
            if not base_url:
                return None
            model_name = self.settings.ai_model
            api_key = self.settings.ai_api_key
            capabilities = ModelCapabilities(
                image_input=self.settings.ai_primary_supports_image,
                structured_output=self.settings.ai_primary_supports_structured_output,
                strict_json_schema=False,
            )
            name = "configured-primary"
        else:
            base_url = self.settings.ai_vision_base_url
            if not base_url:
                return None
            model_name = self.settings.ai_vision_model
            api_key = self.settings.ai_vision_api_key or self.settings.ai_api_key
            capabilities = ModelCapabilities(
                image_input=True,
                structured_output=self.settings.ai_vision_supports_structured_output,
            )
            name = "configured-vision"
        if not self.settings.external_ai_allowed and not _is_local_url(base_url):
            raise PermissionError("当前策略禁止向外部 AI 服务发送邮件内容")
        return OpenAICompatibleProvider(
            ModelProfile(
                name=name,
                base_url=base_url,
                api_key=api_key,
                model_name=model_name,
                capabilities=capabilities,
            )
        )


def _is_local_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
