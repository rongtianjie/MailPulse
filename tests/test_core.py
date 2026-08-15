from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from mailpulse.ai.orchestrator import AIOrchestrator, ModelRouter
from mailpulse.ai.profile_service import AIProfileService
from mailpulse.ai.providers import OpenAICompatibleProvider
from mailpulse.ai.types import (
    GenerationRequest,
    GenerationResult,
    ImagePart,
    ModelCapabilities,
    ModelProfile,
    ModelRuntimePolicy,
    SourceReference,
    StructuredSummary,
    TextPart,
    parse_json_text,
)
from mailpulse.attachments.converter import MarkItDownAttachmentConverter
from mailpulse.auth import create_user
from mailpulse.config import Settings, get_settings
from mailpulse.db import build_session_factory, init_database
from mailpulse.delivery import ReportDeliveryService
from mailpulse.demo import seed_demo
from mailpulse.filtering import RuleEvaluator, RuleValidationError
from mailpulse.mail.connectors import FakeMailConnector
from mailpulse.mail.sync import MailSyncService
from mailpulse.mail.types import RawAttachment, RawMessage
from mailpulse.models import (
    AIProviderProfile,
    Attachment,
    AuditLog,
    CanonicalMessage,
    JobRun,
    Mailbox,
    ModelBinding,
    Report,
    Schedule,
)
from mailpulse.report_service import ReportService
from mailpulse.reports import render_summary_markdown
from mailpulse.search import SearchService
from mailpulse.security import decrypt_secret, encrypt_secret, verify_password
from mailpulse.web.rate_limit import LoginRateLimiter
from mailpulse.worker import _due_fire_time, _run_schedule, build_cron_expression


def make_settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        secret_key="test-secret-key",
        credential_key="test-credential-key",
    )


def test_password_and_credential_round_trip(tmp_path):
    settings = make_settings(tmp_path)
    encrypted = encrypt_secret("mail-password", settings)
    assert encrypted != "mail-password"
    assert decrypt_secret(encrypted, settings) == "mail-password"

    from mailpulse.security import hash_password

    password_hash = hash_password("correct-password")
    assert verify_password("correct-password", password_hash)
    assert not verify_password("wrong-password", password_hash)


def test_parse_json_text_accepts_singleton_object_array_from_compatible_server():
    assert parse_json_text('[{"summary":"ok"}]') == {"summary": "ok"}
    assert parse_json_text('[{"summary":"ok"},{"extra":true}]') is None


def test_database_initialization_is_alembic_compatible(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    engine = __import__("mailpulse.db", fromlist=["build_engine"]).build_engine(settings)
    assert "alembic_version" in inspect(engine).get_table_names()
    assert engine.connect().exec_driver_sql("select count(*) from users").scalar_one() == 0


def test_production_settings_require_explicit_secrets(tmp_path):
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, environment="production")
    settings = Settings(
        data_dir=tmp_path,
        environment="production",
        secret_key="production-secret",
        credential_key="production-credential",
    )
    assert settings.environment == "production"


def test_rule_evaluator_is_safe_and_supports_nested_conditions():
    evaluator = RuleEvaluator()
    message = RawMessage(
        message_id="<one@example.com>",
        subject="重要项目通知",
        sender="project@example.com",
        recipients=["user@example.com"],
        cc=[],
        received_at=datetime.now(UTC),
        body_text="请在周五前确认排期。",
        thread_key=None,
    )
    rule = {
        "kind": "group",
        "operator": "and",
        "children": [
            {"kind": "condition", "field": "subject", "operator": "contains", "value": "项目"},
            {
                "kind": "condition",
                "field": "sender",
                "operator": "regex",
                "value": r"@example\.com$",
            },
        ],
    }
    assert evaluator.evaluate(rule, message)
    with pytest.raises(RuleValidationError):
        evaluator.evaluate(
            {"kind": "condition", "field": "subject", "operator": "regex", "value": "["},
            message,
        )


def test_rule_evaluator_reads_database_attachment_size():
    attachment = Attachment(
        filename="large.bin", mime_type="application/octet-stream", size_bytes=4096
    )
    rule = {
        "kind": "condition",
        "field": "attachment_size",
        "operator": "greater_than",
        "value": 1000,
    }
    assert RuleEvaluator().evaluate(rule, {"attachments": [attachment]})


def test_uidvalidity_change_does_not_duplicate_canonical_message(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "user@example.com", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address=user.email,
            imap_host="fake",
            username=user.email,
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        raw = RawMessage(
            message_id="<same@example.com>",
            subject="同一封邮件",
            sender="sender@example.com",
            recipients=[user.email],
            cc=[],
            received_at=datetime.now(UTC),
            body_text="正文",
            thread_key=None,
        )
        service = MailSyncService(db, settings)
        first = service.sync(mailbox, FakeMailConnector([raw], uid_validity="one"))
        second = service.sync(mailbox, FakeMailConnector([raw], uid_validity="one"))
        third = service.sync(mailbox, FakeMailConnector([raw], uid_validity="two"))
        assert first.created == 1
        assert second.fetched == 0
        assert third.created == 0 and third.linked == 1
        assert db.query(Mailbox).one().sync_uid_validity == "two"
        assert db.query(Mailbox).one().sync_last_uid == 1
        assert (
            db.query(
                __import__("mailpulse.models", fromlist=["CanonicalMessage"]).CanonicalMessage
            ).count()
            == 1
        )
    finally:
        db.close()


def test_attachment_limits_quota_count_and_safe_filename(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        secret_key="test-secret-key",
        credential_key="test-credential-key",
        max_attachment_bytes=1024,
        max_attachments_per_message=1,
        max_user_storage_bytes=1024,
        max_global_storage_bytes=4096,
    )
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "quota@example.com", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address=user.email,
            imap_host="fake",
            username=user.email,
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        payload = b"x" * 700
        messages = [
            RawMessage(
                message_id="<quota-1@example.com>",
                subject="第一封",
                sender="sender@example.com",
                recipients=[user.email],
                cc=[],
                received_at=datetime.now(UTC),
                body_text="正文2",
                thread_key=None,
                attachments=[
                    RawAttachment("../../safe.txt", "text/plain", payload),
                    RawAttachment("ignored.txt", "text/plain", b"second"),
                ],
            ),
            RawMessage(
                message_id="<quota-2@example.com>",
                subject="第二封",
                sender="sender@example.com",
                recipients=[user.email],
                cc=[],
                received_at=datetime.now(UTC),
                body_text="正文",
                thread_key=None,
                attachments=[RawAttachment("quota.txt", "text/plain", payload)],
            ),
        ]
        MailSyncService(db, settings).sync(mailbox, FakeMailConnector(messages))
        db.flush()
        attachments = list(db.scalars(select(Attachment).order_by(Attachment.id)))
        assert [item.conversion_status for item in attachments] == [
            "pending",
            "too_many",
            "storage_limit",
        ]
        assert attachments[0].filename == "safe.txt"
        assert Path(attachments[0].storage_path).is_file()
        assert attachments[2].storage_path is None
    finally:
        db.close()


def test_login_rate_limiter_expires_and_clears_attempts():
    limiter = LoginRateLimiter(max_failures=2, window_seconds=10)
    limiter.record_failure("client", now=0)
    limiter.record_failure("client", now=1)
    assert limiter.allowed("client", now=2) is False
    assert limiter.allowed("client", now=11) is True
    limiter.record_failure("client", now=12)
    limiter.clear("client")
    assert limiter.allowed("client", now=12) is True


def test_search_falls_back_when_fts_query_is_invalid(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "search@example.com", "password-123")
        message = __import__("mailpulse.models", fromlist=["CanonicalMessage"]).CanonicalMessage(
            owner_user_id=user.id,
            content_hash="search-hash",
            subject="搜索测试",
            sender="sender@example.com",
            recipients=[user.email],
            cc=[],
            body_text="包含搜索关键词",
        )
        db.add(message)
        db.flush()
        SearchService(db).index_message(message)
        results = SearchService(db).search(user.id, '"unterminated')
        assert results == []
        assert SearchService(db).search(user.id, "搜索") == [message]
    finally:
        db.close()


def test_message_sync_survives_unavailable_fts_index(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "search-fallback@example.com", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address=user.email,
            imap_host="fake",
            username=user.email,
            credential_encrypted=encrypt_secret("mail-password", settings),
        )
        db.add(mailbox)
        db.flush()
        db.execute(text("DROP TABLE message_search"))
        message = RawMessage(
            message_id="<fts-fallback@example.com>",
            subject="没有 FTS 也要可搜索",
            sender="sender@example.com",
            recipients=[user.email],
            cc=[],
            received_at=datetime.now(UTC),
            body_text="普通字段查询仍然可以找到这封邮件",
            thread_key=None,
        )
        result = MailSyncService(db, settings).sync(mailbox, FakeMailConnector([message]))
        assert result.created == 1
        db.commit()
        found = SearchService(db).search(user.id, "可搜索")
        assert len(found) == 1
        assert found[0].subject == message.subject
    finally:
        db.close()


def test_ai_request_retries_transient_http_failure(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        if len(calls) == 1:
            return httpx.Response(503, request=httpx.Request("POST", "http://test"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"summary":"ok"}'}}],
                "usage": {},
            },
            request=httpx.Request("POST", "http://test"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = OpenAICompatibleProvider(
        ModelProfile(
            name="test",
            base_url="http://test/v1",
            api_key=None,
            model_name="test-model",
            capabilities=ModelCapabilities(structured_output=False),
        )
    )
    result = provider.generate(
        GenerationRequest(role="test", content_parts=[TextPart("hello")], retries=1)
    )
    assert result.parsed_json == {"summary": "ok"}
    assert len(calls) == 2


def test_image_content_preserves_converted_asset_mime_type(tmp_path):
    image = tmp_path / "scan.jpg"
    image.write_bytes(b"fake-jpeg")
    content = OpenAICompatibleProvider._content(
        [ImagePart(path=image, mime_type="image/jpeg", source_name="attachment-1")]
    )
    assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_ai_source_validation_discards_unknown_evidence():
    messages = [RawMessage("1", "subject", "sender", [], [], None, "body", None)]
    from mailpulse.attachments.converter import ConvertedAttachment

    attachments = [(2, ConvertedAttachment(2, None, "", [], [], "converted", "test"))]
    parsed = {
        "evidence": [
            {"message_id": 1, "attachment_id": 2, "extracted_text": "valid"},
            {"message_id": 999, "attachment_id": 2, "extracted_text": "invalid"},
        ]
    }
    evidence = AIOrchestrator._parse_evidence(parsed, messages, attachments)
    assert [item.extracted_text for item in evidence] == ["valid"]


def test_markitdown_converts_attachment_to_markdown(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "attachment@example.com", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address=user.email,
            imap_host="fake",
            username=user.email,
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        raw = RawMessage(
            message_id="<attachment@example.com>",
            subject="附件",
            sender="sender@example.com",
            recipients=[user.email],
            cc=[],
            received_at=datetime.now(UTC),
            body_text="请读取附件",
            thread_key=None,
            attachments=[RawAttachment("notes.txt", "text/plain", "截止日期：2026-08-28".encode())],
        )
        MailSyncService(db, settings).sync(mailbox, FakeMailConnector([raw]))
        db.flush()
        attachment = db.scalar(select(Attachment))
        assert attachment is not None
        result = MarkItDownAttachmentConverter(settings).convert(db, attachment)
        assert result.status == "converted"
        assert "截止日期" in result.markdown_content
        assert attachment.markdown_path
    finally:
        db.close()


def test_demo_sync_to_report_records_markdown_status_and_audit(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "report@example.com", "password-123")
        seed_demo(db, user, settings.data_dir)
        report = ReportService(db, settings).generate_for_user(user, use_demo_provider=True)
        db.commit()
        assert report.status == "success"
        assert "附件处理状态" in report.rendered_markdown
        audit_actions = [row.action for row in db.query(AuditLog)]
        assert "ai_generate" in audit_actions
    finally:
        db.close()


def test_rendered_report_contains_source_locations():
    summary = StructuredSummary(
        summary="有来源的摘要",
        action_items=[{"action": "确认排期", "source_refs": ["message:1"], "verified": True}],
        source_refs=[
            SourceReference(
                message_id=1,
                attachment_id=2,
                page_number=3,
                image_index=1,
                quote="截止日期",
            )
        ],
    )
    rendered = render_summary_markdown(
        summary, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert "来源：message:1" in rendered
    assert "邮件 1，附件 2，第 3 页，图片 1，摘录：截止日期" in rendered


class RecordingProvider:
    def __init__(self, name: str, response: dict, error: Exception | None = None):
        self.name = name
        self.response = response
        self.error = error
        self.roles: list[str] = []
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.roles.append(request.role)
        self.requests.append(request)
        if self.error:
            raise self.error
        return GenerationResult(str(self.response), self.response, self.name)


def test_model_profiles_apply_separate_runtime_policies(tmp_path):
    image_one = tmp_path / "one.png"
    image_two = tmp_path / "two.png"
    image_one.write_bytes(b"one")
    image_two.write_bytes(b"two")
    primary_response = {"summary": "按视觉证据完成归纳", "action_items": [], "source_refs": []}
    primary = RecordingProvider("primary", primary_response)
    primary.profile = ModelProfile(
        name="primary",
        base_url="http://local/v1",
        api_key=None,
        model_name="primary",
        policy=ModelRuntimePolicy(
            max_input_chars=10_000,
            max_output_tokens=321,
            timeout_seconds=11,
            max_retries=1,
        ),
    )
    vision = RecordingProvider("vision", {"evidence": []})
    vision.profile = ModelProfile(
        name="vision",
        base_url="http://local/v1",
        api_key=None,
        model_name="vision",
        policy=ModelRuntimePolicy(
            max_input_chars=8_000,
            max_output_tokens=222,
            timeout_seconds=12,
            max_retries=0,
            max_images=1,
        ),
    )
    orchestrator = AIOrchestrator(ModelRouter(primary, primary_image_input=False, vision=vision))
    from mailpulse.attachments.converter import ConvertedAttachment

    orchestrator.summarize(
        [RawMessage("1", "图片", "sender@example.com", [], [], None, "正文", None)],
        [
            (
                2,
                ConvertedAttachment(
                    2,
                    None,
                    "图片附件",
                    [
                        {"path": str(image_one), "mime_type": "image/png"},
                        {"path": str(image_two), "mime_type": "image/png"},
                    ],
                    [],
                    "converted",
                    "test",
                ),
            )
        ],
    )
    assert vision.requests[0].max_output_tokens == 222
    assert vision.requests[0].timeout == 12
    assert isinstance(vision.requests[0].content_parts[0], TextPart)
    assert "只输出一个 JSON 对象" in vision.requests[0].content_parts[0].text
    image_content = [
        part for part in vision.requests[0].content_parts if isinstance(part, ImagePart)
    ]
    assert len(image_content) == 1
    assert primary.requests[0].max_output_tokens == 321
    assert primary.requests[0].timeout == 11
    assert isinstance(primary.requests[0].content_parts[0], TextPart)
    assert (
        "只输出一个符合给定 JSON Schema 的 JSON 对象"
        in primary.requests[0].content_parts[0].text
    )


def test_ai_router_uses_vision_for_text_only_primary(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    primary_response = {
        "category": "测试",
        "priority": "normal",
        "summary": "已完成识别",
        "action_items": [],
        "decisions": [],
        "risks": [],
        "questions": [],
        "source_refs": [],
        "attachment_status": [],
    }
    vision_response = {
        "evidence": [{"message_id": 1, "attachment_id": 2, "extracted_text": "图片文字"}]
    }
    primary = RecordingProvider("primary", primary_response)
    vision = RecordingProvider("vision", vision_response)
    orchestrator = AIOrchestrator(ModelRouter(primary, primary_image_input=False, vision=vision))
    message = RawMessage(
        "<ai@example.com>", "图片", "sender@example.com", [], [], datetime.now(UTC), "正文", None
    )
    from mailpulse.attachments.converter import ConvertedAttachment

    summary, trace = orchestrator.summarize(
        [message],
        [
            (
                2,
                ConvertedAttachment(
                    2,
                    None,
                    "图片附件",
                    [{"path": str(image), "mime_type": "image/png"}],
                    [],
                    "converted",
                    "test",
                ),
            )
        ],
    )
    assert summary.summary == "已完成识别"
    assert vision.roles == ["vision_extractor"]
    assert primary.roles == ["primary_summarizer"]
    assert trace["used_vision"] is True


def test_ai_router_degrades_when_vision_provider_fails(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"fake-image")
    primary_response = {
        "summary": "仅根据可验证文本完成归纳",
        "action_items": [],
        "source_refs": [],
    }
    primary = RecordingProvider("primary", primary_response)
    vision = RecordingProvider("vision", {}, error=TimeoutError("vision timeout"))
    orchestrator = AIOrchestrator(ModelRouter(primary, primary_image_input=False, vision=vision))
    from mailpulse.attachments.converter import ConvertedAttachment

    summary, trace = orchestrator.summarize(
        [RawMessage("1", "图片", "sender@example.com", [], [], None, "正文", None)],
        [
            (
                2,
                ConvertedAttachment(
                    2,
                    None,
                    "图片附件",
                    [{"path": str(image), "mime_type": "image/png"}],
                    [],
                    "converted",
                    "test",
                ),
            )
        ],
    )
    assert summary.summary == "仅根据可验证文本完成归纳"
    assert trace["vision_error"] == "TimeoutError"
    assert primary.roles == ["primary_summarizer"]


def test_model_profile_global_binding_is_resolved(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "profile@example.com", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address=user.email,
            imap_host="fake",
            username=user.email,
            credential_encrypted=encrypt_secret("secret", settings),
        )
        profile = AIProviderProfile(
            name="global-primary",
            base_url="http://127.0.0.1:8000/v1",
            model_name="test-model",
            capabilities={"image_input": True, "structured_output": True},
            policy={"max_output_tokens": 777, "max_retries": 1},
        )
        db.add_all([mailbox, profile])
        db.flush()
        db.add(ModelBinding(role="primary", provider_profile_id=profile.id))
        db.commit()
        resolved = AIProfileService(db, settings).resolve_for(user.id, mailbox.id)
        assert resolved.primary is not None
        assert resolved.primary_image_input is True
        assert resolved.primary.profile.policy.max_output_tokens == 777
        assert resolved.primary.profile.policy.max_retries == 1
    finally:
        db.close()


def test_schedule_due_time_and_cron_presets():
    schedule = Schedule(
        timezone="Asia/Shanghai",
        cron_expression="0 9 * * *",
        lookback_hours=24,
    )
    before = datetime(2026, 8, 15, 0, 30, tzinfo=UTC)
    after = datetime(2026, 8, 15, 1, 30, tzinfo=UTC)
    assert _due_fire_time(schedule, before) is None
    fire = _due_fire_time(schedule, after)
    assert fire is not None
    assert fire.hour == 9 and fire.utcoffset() == timedelta(hours=8)
    schedule.last_run_at = fire.astimezone(UTC)
    assert _due_fire_time(schedule, after) is None
    assert build_cron_expression("weekly", "08:30", "mon,wed,fri") == "30 8 * * mon,wed,fri"
    assert build_cron_expression("custom", "09:00", custom_cron="5 10 * * 1-5") == "5 10 * * 1-5"


def test_worker_persists_safe_failure_state(tmp_path, monkeypatch):
    import mailpulse.worker as worker_module

    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "worker@example.com", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address=user.email,
            imap_host="fake",
            username=user.email,
            credential_encrypted=encrypt_secret("worker-secret", settings),
        )
        db.add(mailbox)
        db.flush()
        now = datetime.now(UTC)
        schedule = Schedule(
            user_id=user.id,
            mailbox_id=mailbox.id,
            cron_expression="0 9 * * *",
            timezone="UTC",
            lookback_hours=24,
            last_run_at=now - timedelta(days=1),
        )
        db.add(schedule)
        db.flush()
        db.commit()

        class FailingConnector:
            def __init__(self, connection):
                self.connection = connection

            def sync_messages(self, cursor=None):
                raise ConnectionError("fake mailbox failure")

        monkeypatch.setattr(worker_module, "IMAPConnector", FailingConnector)
        assert _run_schedule(db, schedule, now, now, settings) is False
        job = db.query(JobRun).one()
        assert job.status == "failed"
        assert job.stage == "sync"
        assert "worker-secret" not in (job.error_message or "")
    finally:
        db.close()


class FakeDeliveryProvider:
    def __init__(self, error: bool = False):
        self.error = error
        self.calls = []

    def send(self, sender, recipient, subject, body):
        if self.error:
            raise ConnectionError("fake smtp failure")
        self.calls.append((sender, recipient, subject, body))


def test_report_delivery_records_failure_and_retry(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "delivery@example.com", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address=user.email,
            imap_host="fake",
            smtp_host="fake-smtp",
            username=user.email,
            credential_encrypted=encrypt_secret("smtp-secret", settings),
        )
        db.add(mailbox)
        db.flush()
        report = Report(
            user_id=user.id,
            mailbox_id=mailbox.id,
            run_key="manual:test-delivery",
            period_start=datetime.now(UTC) - timedelta(hours=1),
            period_end=datetime.now(UTC),
            status="success",
            title="测试报告",
            rendered_markdown="正文不应写入错误日志",
        )
        db.add(report)
        db.flush()
        failed = ReportDeliveryService(db).send_report(
            report, mailbox, "recipient@example.com", FakeDeliveryProvider(error=True)
        )
        db.commit()
        assert failed.status == "failed"
        assert failed.attempts == 1
        assert "smtp-secret" not in (failed.error_message or "")
        provider = FakeDeliveryProvider()
        retried = ReportDeliveryService(db).retry_delivery(failed, report, mailbox, provider)
        db.commit()
        assert retried.status == "sent"
        assert retried.attempts == 2
        assert provider.calls[0][1] == "recipient@example.com"
    finally:
        db.close()


def test_web_login_csrf_and_demo_report(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "web-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    user = create_user(db, "web@example.com", "password-123", role="admin")
    db.commit()
    db.close()
    client = TestClient(app)
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"email": user.email, "password": "password-123", "csrf_token": token},
    )
    assert response.status_code == 200
    assert "快速开始" in response.text
    assert "载入演示数据" not in response.text
    assert "生成演示报告" not in response.text
    assert "当前可使用演示 Provider" not in response.text
    assert "生成报告" in response.text
    assert client.post("/demo/seed", data={}).status_code == 403
    dashboard = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    admin_page = client.get("/admin")
    assert admin_page.status_code == 200
    admin_token = re.search(r'name="csrf_token" value="([^"]+)"', admin_page.text).group(1)
    created_user_response = client.post(
        "/admin/users",
        data={
            "email": "managed@example.com",
            "display_name": "受管用户",
            "password": "Managed-pass-123",
            "role": "user",
            "csrf_token": admin_token,
        },
        follow_redirects=False,
    )
    assert created_user_response.status_code == 303
    assert "managed@example.com" in client.get("/admin").text
    model_page = client.get("/admin")
    model_token = re.search(r'name="csrf_token" value="([^"]+)"', model_page.text).group(1)
    model_response = client.post(
        "/admin/models",
        data={
            "name": "本地主模型",
            "role": "primary",
            "base_url": "http://127.0.0.1:8000/v1",
            "model_name": "mlx-test",
            "api_key": "test-api-key",
            "image_input": "true",
            "structured_output": "true",
            "timeout_seconds": "45",
            "max_retries": "1",
            "max_input_chars": "64000",
            "max_output_tokens": "900",
            "max_images": "8",
            "max_image_size_mb": "6",
            "csrf_token": model_token,
        },
        follow_redirects=False,
    )
    assert model_response.status_code == 303
    model_db = build_session_factory(settings)()
    try:
        saved_profile = model_db.query(AIProviderProfile).one()
        assert saved_profile.policy["max_output_tokens"] == 900
        assert saved_profile.policy["max_image_bytes"] == 6 * 1024 * 1024
    finally:
        model_db.close()
    assert client.post("/demo/seed", data={"csrf_token": token}).status_code == 200
    monkeypatch.setattr("mailpulse.web.routes.IMAPConnector.test_connection", lambda self: None)
    settings_page = client.get("/settings")
    settings_token = re.search(r'name="csrf_token" value="([^"]+)"', settings_page.text).group(1)
    tested_settings = client.post("/settings/test", data={"csrf_token": settings_token})
    assert tested_settings.status_code == 200
    assert "IMAP 连接验证成功" in tested_settings.text
    assert "测试 IMAP 连接" not in tested_settings.text
    messages_page = client.get("/messages")
    message_db = build_session_factory(settings)()
    message_id = message_db.query(CanonicalMessage).order_by(CanonicalMessage.id).first().id
    message_db.close()
    message_token = re.search(r'name="csrf_token" value="([^"]+)"', messages_page.text).group(1)
    assert (
        client.post(
            f"/messages/{message_id}/toggle-processed",
            data={"csrf_token": message_token},
        ).status_code
        == 200
    )
    verify_message_db = build_session_factory(settings)()
    try:
        assert verify_message_db.get(CanonicalMessage, message_id).local_processed is True
    finally:
        verify_message_db.close()
    dashboard = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    report_response = client.post(
        "/reports/generate",
        data={"use_demo_provider": "true", "csrf_token": token},
    )
    assert report_response.status_code == 200
    reports_page_text = client.get("/reports").text
    assert "生成报告" in reports_page_text
    assert "生成演示报告" not in reports_page_text
    report_detail_page = client.get("/reports/1")
    assert report_detail_page.status_code == 200
    assert '"used_vision": false' in report_detail_page.text
    schedules_page = client.get("/schedules")
    assert schedules_page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', schedules_page.text).group(1)
    schedule_db = build_session_factory(settings)()
    mailbox = schedule_db.query(Mailbox).filter(Mailbox.user_id == user.id).one()
    schedule_db.close()
    schedule_response = client.post(
        "/schedules",
        data={
            "name": "工作日邮件报告",
            "mailbox_id": mailbox.id,
            "schedule_type": "weekly",
            "scheduled_time": "09:30",
            "weekdays": "mon-fri",
            "timezone": "Asia/Shanghai",
            "lookback_hours": "24",
            "is_enabled": "on",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert schedule_response.status_code == 303
    verify_db = build_session_factory(settings)()
    try:
        saved_schedule = verify_db.query(Schedule).one()
        assert saved_schedule.cron_expression == "30 9 * * mon-fri"
        assert saved_schedule.is_enabled is True
        report_id = verify_db.query(Report).one().id
        other_user = create_user(verify_db, "other@example.com", "password-123")
        verify_db.commit()
    finally:
        verify_db.close()
    other_client = TestClient(app)
    other_login = other_client.get("/login")
    other_token = re.search(r'name="csrf_token" value="([^"]+)"', other_login.text).group(1)
    other_client.post(
        "/login",
        data={
            "email": other_user.email,
            "password": "password-123",
            "csrf_token": other_token,
        },
    )
    assert other_client.get(f"/reports/{report_id}").status_code == 404
    assert other_client.get("/admin").status_code == 403
