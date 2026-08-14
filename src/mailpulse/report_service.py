from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai.demo_provider import DemoProvider
from .ai.orchestrator import AIOrchestrator, ModelRouter
from .ai.providers import OpenAICompatibleProvider
from .ai.types import ModelCapabilities, ModelProfile
from .attachments.converter import MarkItDownAttachmentConverter
from .config import Settings, get_settings
from .mail.types import RawMessage
from .models import Attachment, CanonicalMessage, Mailbox, Report, User
from .reports import render_summary_markdown


class ReportService:
    def __init__(self, session: Session, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    def generate_for_user(self, user: User, use_demo_provider: bool = False) -> Report:
        messages = list(
            self.session.scalars(
                select(CanonicalMessage)
                .where(CanonicalMessage.owner_user_id == user.id)
                .order_by(CanonicalMessage.received_at.desc())
                .limit(100)
            )
        )
        if not messages:
            raise ValueError("当前用户没有可归纳的邮件，请先同步邮箱或生成演示数据")
        mailbox = self.session.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
        if mailbox is None:
            raise ValueError("当前用户尚未配置邮箱")

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
                message_id=message.message_id,
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
        orchestrator = self._build_orchestrator(use_demo_provider)
        summary, trace = orchestrator.summarize(raw_messages, converted)
        end = datetime.now(UTC)
        start = min(
            (message.received_at for message in messages if message.received_at),
            default=end - timedelta(days=1),
        )
        report = Report(
            user_id=user.id,
            mailbox_id=mailbox.id,
            run_key=f"manual:{user.id}:{end.timestamp()}",
            period_start=start,
            period_end=end,
            status="success",
            title="邮件归纳报告",
            summary=summary.model_dump(mode="json"),
            rendered_markdown=render_summary_markdown(summary, start, end),
            model_trace=trace,
        )
        self.session.add(report)
        self.session.flush()
        return report

    def _build_orchestrator(self, use_demo_provider: bool) -> AIOrchestrator:
        if use_demo_provider or not self.settings.ai_base_url:
            primary = DemoProvider()
            return AIOrchestrator(
                ModelRouter(primary, primary_image_input=False), max_output_tokens=1800
            )

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
        )


def _is_local_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
