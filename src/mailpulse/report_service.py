from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai.demo_provider import DemoProvider
from .ai.orchestrator import AIOrchestrator, ModelRouter
from .ai.profile_service import AIProfileService
from .ai.providers import OpenAICompatibleProvider
from .ai.types import ModelCapabilities, ModelProfile
from .attachments.converter import MarkItDownAttachmentConverter
from .config import Settings, get_settings
from .mail.types import RawMessage
from .models import Attachment, AuditLog, CanonicalMessage, Mailbox, Report, RuleSet, Task, User
from .reports import render_summary_markdown
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
        query = select(CanonicalMessage).where(CanonicalMessage.owner_user_id == user.id)
        if period_start is not None:
            query = query.where(
                CanonicalMessage.received_at >= period_start,
                CanonicalMessage.received_at <= period_end,
            )
        all_messages = list(
            self.session.scalars(
                query.order_by(CanonicalMessage.received_at.desc()).limit(
                    self.settings.max_messages_per_report
                )
            )
        )
        rule_sets = list(
            self.session.scalars(
                select(RuleSet)
                .where(RuleSet.task_id == task.id)
                .order_by(RuleSet.priority.asc(), RuleSet.id.asc())
            )
        )
        messages = RuleService(self.session).filter_messages_any(all_messages, rule_sets)
        if not messages:
            raise ValueError("当前时间范围内没有符合规则的邮件")

        converted = []
        converter = MarkItDownAttachmentConverter(self.settings)
        for message in messages:
            for attachment in self.session.scalars(
                select(Attachment).where(Attachment.message_id == message.id)
            ):
                result = converter.convert(self.session, attachment)
                converted.append((attachment.id, result))

        raw_messages = [
            RawMessage(
                message_id=str(message.id),
                subject=message.subject,
                sender=message.sender,
                recipients=message.recipients,
                cc=message.cc,
                received_at=message.received_at,
                body_text=message.body_text,
                thread_key=message.thread_key,
            )
            for message in messages
        ]
        orchestrator = self._build_orchestrator(user, mailbox.id, use_demo_provider)
        summary, trace = orchestrator.summarize(raw_messages, converted)
        end = period_end
        start = min(
            (message.received_at for message in messages if message.received_at),
            default=period_start or end - timedelta(days=1),
        )
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
        report = Report(
            user_id=user.id,
            mailbox_id=mailbox.id,
            task_id=task.id,
            run_key=run_key or f"manual:{user.id}:{uuid4().hex}",
            period_start=start,
            period_end=end,
            status="success",
            title="邮件归纳报告",
            summary=summary_payload,
            rendered_markdown=render_summary_markdown(summary, start, end),
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
            )
        resolved = AIProfileService(self.session, self.settings).resolve_for(user.id, mailbox_id)
        if resolved.primary:
            return AIOrchestrator(
                ModelRouter(
                    resolved.primary,
                    primary_image_input=resolved.primary_image_input,
                    vision=resolved.vision,
                ),
                max_output_tokens=self.settings.ai_max_output_tokens,
                timeout=self.settings.ai_timeout_seconds,
                max_input_chars=self.settings.ai_max_input_chars,
                retries=self.settings.ai_max_retries,
            )
        if not self.settings.ai_base_url:
            raise RuntimeError("尚未配置 AI 主模型，请在管理控制台配置或设置 MAILPULSE_AI_BASE_URL")

        if not self.settings.external_ai_allowed and not _is_local_url(self.settings.ai_base_url):
            raise PermissionError("当前策略禁止向外部 AI 服务发送邮件内容")
        primary_profile = ModelProfile(
            name="configured-primary",
            base_url=self.settings.ai_base_url,
            api_key=self.settings.ai_api_key,
            model_name=self.settings.ai_model,
            capabilities=ModelCapabilities(
                image_input=self.settings.ai_primary_supports_image,
                structured_output=self.settings.ai_primary_supports_structured_output,
                strict_json_schema=False,
            ),
        )
        primary = OpenAICompatibleProvider(primary_profile)
        vision = None
        if self.settings.ai_vision_base_url:
            if not self.settings.external_ai_allowed and not _is_local_url(
                self.settings.ai_vision_base_url
            ):
                raise PermissionError("当前策略禁止向外部视觉 AI 服务发送邮件内容")
            vision_profile = ModelProfile(
                name="configured-vision",
                base_url=self.settings.ai_vision_base_url,
                api_key=self.settings.ai_vision_api_key or self.settings.ai_api_key,
                model_name=self.settings.ai_vision_model,
                capabilities=ModelCapabilities(
                    image_input=True,
                    structured_output=self.settings.ai_vision_supports_structured_output,
                ),
            )
            vision = OpenAICompatibleProvider(vision_profile)
        return AIOrchestrator(
            ModelRouter(
                primary, primary_image_input=self.settings.ai_primary_supports_image, vision=vision
            ),
            max_output_tokens=self.settings.ai_max_output_tokens,
            timeout=self.settings.ai_timeout_seconds,
            max_input_chars=self.settings.ai_max_input_chars,
            retries=self.settings.ai_max_retries,
        )


def _is_local_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
