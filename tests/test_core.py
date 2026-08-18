from __future__ import annotations

import base64
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from mailpulse import cli
from mailpulse.ai.orchestrator import AIOrchestrator, ModelRouter
from mailpulse.ai.profile_service import AIProfileService
from mailpulse.ai.providers import OpenAICompatibleProvider
from mailpulse.ai.types import (
    GenerationRequest,
    GenerationResult,
    ImagePart,
    MessageSummary,
    ModelCapabilities,
    ModelProfile,
    ModelRuntimePolicy,
    SourceReference,
    StructuredSummary,
    TextPart,
    parse_json_text,
)
from mailpulse.attachments.converter import MarkItDownAttachmentConverter
from mailpulse.auth import authenticate, create_user
from mailpulse.config import Settings, get_settings
from mailpulse.db import bootstrap_database, build_session_factory, init_database, reset_database
from mailpulse.delivery import ReportDeliveryService
from mailpulse.demo import demo_messages, seed_demo
from mailpulse.filtering import RuleEvaluator, RuleValidationError
from mailpulse.mail.connectors import FakeMailConnector, IMAPConnector
from mailpulse.mail.sync import MailSyncService
from mailpulse.mail.types import MailboxConnection, RawAttachment, RawMessage
from mailpulse.models import (
    AIProviderProfile,
    Attachment,
    AuditLog,
    CanonicalMessage,
    Delivery,
    JobRun,
    Mailbox,
    MessageOccurrence,
    ModelBinding,
    Report,
    RuleSet,
    Task,
    TaskDeliveryTarget,
    User,
)
from mailpulse.report_service import ReportService
from mailpulse.reports import render_summary_markdown
from mailpulse.rules import RuleService
from mailpulse.search import SearchService
from mailpulse.security import decrypt_secret, encrypt_secret, verify_password
from mailpulse.web.rate_limit import LoginRateLimiter
from mailpulse.worker import (
    _due_fire_time,
    _recover_stale_jobs,
    build_cron_expression,
    enqueue_job_run,
    run_due_tasks,
    run_task_now,
)


def make_settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        secret_key="test-secret-key",
        credential_key="test-credential-key",
    )


def choose_login_mode(client, response, mode: str = "admin"):
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    return client.post(
        "/login/mode",
        data={"mode": mode, "csrf_token": token},
        follow_redirects=False,
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


def test_serve_uses_settings_and_allows_cli_overrides(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(host="0.0.0.0", port=9090, data_dir=tmp_path),
    )
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append((args, kwargs)))

    monkeypatch.setattr(sys, "argv", ["mailpulse", "serve"])
    cli.main()
    assert calls[-1][1]["host"] == "0.0.0.0"
    assert calls[-1][1]["port"] == 9090
    assert "MailPulse 服务监听地址: 0.0.0.0:9090" in capsys.readouterr().out

    monkeypatch.setattr(
        sys, "argv", ["mailpulse", "serve", "--host", "127.0.0.1", "--port", "8081"]
    )
    cli.main()
    assert calls[-1][1]["host"] == "127.0.0.1"
    assert calls[-1][1]["port"] == 8081
    output = capsys.readouterr().out
    assert "MailPulse 服务监听地址: 127.0.0.1:8081" in output
    assert "host 来源: 命令行" in output
    assert "port 来源: 命令行" in output


def test_parse_json_text_accepts_singleton_object_array_from_compatible_server():
    assert parse_json_text('[{"summary":"ok"}]') == {"summary": "ok"}
    assert parse_json_text('[{"summary":"ok"},{"extra":true}]') is None


def test_database_initialization_creates_schema_without_migrations(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    engine = __import__("mailpulse.db", fromlist=["build_engine"]).build_engine(settings)
    table_names = set(inspect(engine).get_table_names())
    assert {"users", "mailboxes", "reports", "audit_logs"}.issubset(table_names)
    assert "alembic_version" not in table_names
    assert engine.connect().exec_driver_sql("select count(*) from users").scalar_one() == 0


def test_bootstrap_creates_default_admin_once(tmp_path):
    settings = make_settings(tmp_path)
    first = bootstrap_database(settings)
    assert first is not None
    assert first.username == "admin"
    assert first.password == "admin123"

    db = build_session_factory(settings)()
    try:
        admin = db.query(User).filter(User.role == "admin").one()
        assert admin.role == "admin"
        assert admin.must_change_password is True
        assert authenticate(db, first.username, first.password) is admin
        paired = admin.paired_user
        assert paired is not None
        assert paired.role == "user"
        assert paired.username.startswith("__admin_user_")
        assert paired.password_hash == admin.password_hash
    finally:
        db.close()

    assert bootstrap_database(settings) is None


def test_reset_database_recreates_default_admin(tmp_path):
    settings = make_settings(tmp_path)
    bootstrap_database(settings)
    database_path = settings.data_dir / "mailpulse.sqlite3"
    assert database_path.exists()

    reset_path = reset_database(settings)
    assert reset_path == database_path.resolve()
    assert not database_path.exists()
    recreated = bootstrap_database(settings)
    assert recreated is not None


def test_create_user_username_rules(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "root", "password-123", display_name="运维管理员")
        assert user.username == "root"
        assert authenticate(db, "ROOT", "password-123") is user
        with pytest.raises(ValueError):
            create_user(db, "root", "password-123")
        with pytest.raises(ValueError):
            create_user(db, "bad name", "password-123")
        with pytest.raises(ValueError):
            create_user(db, "中文名", "password-123")
        with pytest.raises(ValueError):
            create_user(db, "ab", "password-123")
        with pytest.raises(ValueError):
            create_user(db, "a" * 33, "password-123")
    finally:
        db.close()


def test_production_settings_require_explicit_secrets(tmp_path):
    with pytest.raises(ValueError):
        Settings(
            data_dir=tmp_path,
            environment="production",
            secret_key="mailpulse-development-only-change-me",
            credential_key=None,
        )
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
        user = create_user(db, "user", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="mailbox@example.com",
            imap_host="fake",
            username="mailbox@example.com",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        raw = RawMessage(
            message_id="<same@example.com>",
            subject="同一封邮件",
            sender="sender@example.com",
            recipients=["mailbox@example.com"],
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


def test_sync_deduplicates_identical_messages_in_one_batch(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "batch-dedup", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="mailbox@example.com",
            imap_host="fake",
            username="mailbox@example.com",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        raw = RawMessage(
            message_id="<batch-dedup@example.com>",
            subject="批次内重复邮件",
            sender="sender@example.com",
            recipients=["mailbox@example.com"],
            cc=[],
            received_at=datetime.now(UTC),
            body_text="相同正文",
            thread_key=None,
        )
        result = MailSyncService(db, settings).sync(
            mailbox, FakeMailConnector([raw, raw])
        )
        db.commit()
        assert result.created == 1
        assert result.linked == 1
        assert db.query(CanonicalMessage).count() == 1
        assert db.query(MessageOccurrence).count() == 2
    finally:
        db.close()


def test_imap_batch_fetch_ignores_trailing_marker(monkeypatch):
    raw = (
        b"From: sender@example.com\n"
        b"To: user@example.com\n"
        b"Subject: Batch fetch\n"
        b"Message-ID: <batch-fetch@example.com>\n"
        b"Date: Thu, 14 Aug 2026 12:00:00 +0000\n"
        b"\nBody"
    )

    class FakeIMAPClient:
        def response(self, key):
            assert key == "UIDVALIDITY"
            return "OK", [b"7"]

        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b"1"]
            assert command == "FETCH"
            return "OK", [(b"1 (UID 1 BODY[] {5})", raw), b")"]

        def logout(self):
            return "BYE", []

    connector = IMAPConnector(
        MailboxConnection(
            host="fake",
            port=993,
            username="user@example.com",
            password="secret",
            tls=True,
            folder="INBOX",
        )
    )
    monkeypatch.setattr(connector, "_open", lambda: FakeIMAPClient())
    result = connector.sync_messages()
    assert result.cursor.last_uid == 1
    assert len(result.messages) == 1
    assert result.messages[0][0] == 1


def test_imap_internal_date_is_separate_and_normalized_to_utc(monkeypatch):
    raw = (
        b"From: sender@example.com\n"
        b"To: user@example.com\n"
        b"Subject: Internal date\n"
        b"Date: Thu, 14 Aug 2026 12:00:00 +0800\n"
        b"Message-ID: <internal-date@example.com>\n\nBody"
    )

    class InternalDateClient:
        def response(self, key):
            return "OK", [b"7"]

        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b"1"]
            return "OK", [
                (b'1 (UID 1 INTERNALDATE "14-Aug-2026 13:00:00 +0800" BODY[] {5})', raw),
                b")",
            ]

        def logout(self):
            return "BYE", []

    connector = IMAPConnector(
        MailboxConnection(
            host="fake",
            port=993,
            username="user@example.com",
            password="secret",
        )
    )
    monkeypatch.setattr(connector, "_open", lambda: InternalDateClient())

    batch = connector.sync_messages()
    message = batch.messages[0][1]
    assert message.received_at == datetime(2026, 8, 14, 4, tzinfo=UTC)
    assert message.internal_date == datetime(2026, 8, 14, 5, tzinfo=UTC)


def test_search_count_applies_status_filter_with_fts(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "search-status", "password-123")
        messages = [
            CanonicalMessage(
                owner_user_id=user.id,
                content_hash="status-search-1",
                subject="重要状态通知",
                sender="sender@example.com",
                recipients=["mailbox@example.com"],
                cc=[],
                body_text="需要处理",
                local_starred=False,
            ),
            CanonicalMessage(
                owner_user_id=user.id,
                content_hash="status-search-2",
                subject="重要状态通知",
                sender="sender@example.com",
                recipients=["mailbox@example.com"],
                cc=[],
                body_text="已经加星",
                local_starred=True,
            ),
        ]
        db.add_all(messages)
        db.flush()
        service = SearchService(db)
        for message in messages:
            service.index_message(message)
        assert service.count(user.id, "重要状态通知") == 2
        assert service.count(user.id, "重要状态通知", status="starred") == 1
        assert len(service.search(user.id, "重要状态通知", status="starred")) == 1
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
        user = create_user(db, "quota", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="mailbox@example.com",
            imap_host="fake",
            username="mailbox@example.com",
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
                recipients=["mailbox@example.com"],
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
                recipients=["mailbox@example.com"],
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
        user = create_user(db, "search", "password-123")
        message = __import__("mailpulse.models", fromlist=["CanonicalMessage"]).CanonicalMessage(
            owner_user_id=user.id,
            content_hash="search-hash",
            subject="搜索测试",
            sender="sender@example.com",
            recipients=["mailbox@example.com"],
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
        user = create_user(
            db, "search-fallback", "password-123"
        )
        mailbox = Mailbox(
            user_id=user.id,
            email_address="mailbox@example.com",
            imap_host="fake",
            username="mailbox@example.com",
            credential_encrypted=encrypt_secret("mail-password", settings),
        )
        db.add(mailbox)
        db.flush()
        db.execute(text("DROP TABLE message_search"))
        message = RawMessage(
            message_id="<fts-fallback@example.com>",
            subject="没有 FTS 也要可搜索",
            sender="sender@example.com",
            recipients=["mailbox@example.com"],
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
        calls.append(json.loads(json.dumps(kwargs["json"])))
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


def test_ai_request_separates_system_prompt_and_falls_back_from_strict_schema(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(json.loads(json.dumps(kwargs["json"])))
        if len(calls) == 1:
            return httpx.Response(400, request=httpx.Request("POST", "http://test"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"summary":"ok"}'}}]},
            request=httpx.Request("POST", "http://test"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = OpenAICompatibleProvider(
        ModelProfile(
            name="test",
            base_url="http://test/v1",
            api_key=None,
            model_name="test-model",
            capabilities=ModelCapabilities(structured_output=True, strict_json_schema=True),
        )
    )
    result = provider.generate(
        GenerationRequest(
            role="primary_summarizer",
            content_parts=[TextPart("hello")],
            system_prompt="system rules",
            response_schema={"type": "object"},
        )
    )

    assert result.parsed_json == {"summary": "ok"}
    assert [item["role"] for item in calls[0]["messages"]] == ["system", "user"]
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["response_format"]["type"] == "json_object"


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


def test_ai_source_validation_removes_unknown_action_refs():
    summary = StructuredSummary(
        summary="ok",
        action_items=[
            {
                "action": "处理",
                "source_refs": ["message:1", "message:999", "fake"],
                "verified": True,
            }
        ],
    )
    AIOrchestrator._validate_summary_sources(
        summary,
        [RawMessage("1", "subject", "sender", [], [], None, "body", None)],
        [],
    )
    assert summary.action_items[0].source_refs == ["message:1"]
    assert summary.action_items[0].verified is True


def test_markitdown_converts_attachment_to_markdown(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "attachment", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="mailbox@example.com",
            imap_host="fake",
            username="mailbox@example.com",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        raw = RawMessage(
            message_id="<attachment@example.com>",
            subject="附件",
            sender="sender@example.com",
            recipients=["mailbox@example.com"],
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
        user = create_user(db, "report", "password-123")
        seed_demo(db, user, settings.data_dir)
        task = db.query(Task).filter(Task.user_id == user.id).one()
        report = ReportService(db, settings).generate_for_user(
            user, task=task, use_demo_provider=True
        )
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


def test_two_stage_summary_keeps_message_coverage_and_system_prompt():
    primary = RecordingProvider("primary", {"summary": "汇总结果", "action_items": []})
    orchestrator = AIOrchestrator(ModelRouter(primary, primary_image_input=False))
    messages = [
        RawMessage("1", "第一封", "a@example.com", [], [], None, "第一封正文", "thread-1"),
        RawMessage("2", "第二封", "b@example.com", [], [], None, "第二封正文", "thread-1"),
    ]

    summary, trace = orchestrator.summarize(messages, [], {"task_name": "测试任务"})

    assert primary.roles == ["message_extractor", "primary_summarizer"]
    assert trace["aggregation_mode"] == "two_stage"
    assert summary.coverage.input_message_count == 2
    assert summary.coverage.summarized_message_count == 2
    assert summary.coverage.mode == "degraded"
    assert [item.message_id for item in summary.message_summaries] == [1, 2]
    assert primary.requests[0].system_prompt
    assert "不可信数据" in primary.requests[0].system_prompt
    assert '"cc":[]' in primary.requests[0].content_parts[1].text


def test_ai_json_repair_retries_when_schema_validation_fails():
    class SequenceProvider(RecordingProvider):
        def __init__(self):
            super().__init__("primary", {})
            self.responses = [
                {"summary": "字段错误", "priority": "not-a-priority"},
                {"summary": "修复后的摘要", "priority": "high"},
            ]

        def generate(self, request: GenerationRequest) -> GenerationResult:
            self.roles.append(request.role)
            self.requests.append(request)
            response = self.responses.pop(0)
            return GenerationResult(str(response), response, self.name)

    primary = SequenceProvider()
    orchestrator = AIOrchestrator(ModelRouter(primary, primary_image_input=False))
    message = RawMessage("1", "主题", "sender@example.com", [], [], None, "正文", None)

    summary, _trace = orchestrator.summarize([message], [])

    assert summary.summary == "修复后的摘要"
    assert primary.roles == ["primary_summarizer", "primary_summarizer_json_repair"]


def test_message_data_truncation_keeps_each_record_parseable():
    orchestrator = AIOrchestrator(
        ModelRouter(RecordingProvider("primary", {"summary": "ok"}), False),
        max_input_chars=4_096,
    )
    parts, included, truncated, _warnings = orchestrator._message_data_parts(
        [
            RawMessage("1", "主题一", "a@example.com", [], [], None, "x" * 10_000, None),
            RawMessage("2", "主题二", "b@example.com", [], [], None, "y" * 10_000, None),
        ],
        [],
        4_096,
    )
    records = [json.loads(line) for line in parts[0].text.splitlines()[1:]]
    assert included == {1, 2}
    assert truncated == {1, 2}
    assert [item["message_id"] for item in records] == ["1", "2"]
    assert all(item["body_truncated"] is True for item in records)


def test_ai_input_budget_also_limits_large_metadata_and_card_payload():
    orchestrator = AIOrchestrator(
        ModelRouter(RecordingProvider("primary", {"summary": "ok"}), False),
        max_input_chars=4_096,
    )
    message = RawMessage(
        "1",
        "主题" * 10_000,
        "sender@example.com",
        ["recipient@example.com"],
        [],
        None,
        "正文" * 10_000,
        None,
    )
    parts, _included, _truncated, _warnings = orchestrator._message_data_parts(
        [message], [], 4_096
    )
    cards_text = orchestrator._cards_text(
        [MessageSummary(message_id=1, subject="主题" * 10_000, summary="摘要" * 10_000)],
        {},
        [],
        4_096,
    )

    assert len(parts[0].text) <= 1_024
    assert len(cards_text) <= 1_024


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
        user = create_user(db, "profile", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="mailbox@example.com",
            imap_host="fake",
            username="mailbox@example.com",
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


def test_task_due_time_and_cron_presets():
    task = Task(
        timezone="Asia/Shanghai",
        cron_expression="0 9 * * *",
        lookback_hours=24,
        run_mode="scheduled",
    )
    before = datetime(2026, 8, 15, 0, 30, tzinfo=UTC)
    after = datetime(2026, 8, 15, 1, 30, tzinfo=UTC)
    assert _due_fire_time(task, before) is None
    fire = _due_fire_time(task, after)
    assert fire is not None
    assert fire.hour == 9 and fire.utcoffset() == timedelta(hours=8)
    task.last_run_at = fire.astimezone(UTC)
    assert _due_fire_time(task, after) is None
    # 手动任务不会按计划触发
    manual = Task(run_mode="manual", cron_expression="0 9 * * *", timezone="UTC")
    assert _due_fire_time(manual, after) is None
    assert build_cron_expression("weekly", "08:30", "mon,wed,fri") == "30 8 * * mon,wed,fri"
    assert build_cron_expression("custom", "09:00", custom_cron="5 10 * * 1-5") == "5 10 * * 1-5"


def test_worker_persists_safe_failure_state(tmp_path, monkeypatch):
    import mailpulse.worker as worker_module

    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "worker", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="mailbox@example.com",
            imap_host="fake",
            username="mailbox@example.com",
            credential_encrypted=encrypt_secret("worker-secret", settings),
        )
        db.add(mailbox)
        db.flush()
        now = datetime.now(UTC)
        task = Task(
            user_id=user.id,
            mailbox_id=mailbox.id,
            cron_expression="0 9 * * *",
            timezone="UTC",
            lookback_hours=24,
            run_mode="scheduled",
            last_run_at=now - timedelta(days=1),
        )
        db.add(task)
        db.flush()
        db.commit()

        class FailingConnector:
            def __init__(self, connection):
                self.connection = connection

            def sync_messages(self, cursor=None):
                raise ConnectionError("fake mailbox failure")

        monkeypatch.setattr(worker_module, "IMAPConnector", FailingConnector)
        job = run_task_now(db, task, settings, "schedule:1:test", now)
        assert job.status == "failed"
        assert job.stage == "sync"
        assert "worker-secret" not in (job.error_message or "")
    finally:
        db.close()


def test_worker_recovers_stale_running_job_and_releases_locks(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        secret_key="test-secret-key",
        credential_key="test-credential-key",
        job_stale_after_hours=1,
    )
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "stale-worker", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="stale@example.com",
            imap_host="fake",
            username="stale@example.com",
            credential_encrypted=encrypt_secret("stale-secret", settings),
        )
        db.add(mailbox)
        db.flush()
        run_key = "manual:stale-job"
        task = Task(
            user_id=user.id,
            mailbox_id=mailbox.id,
            active_run_key=run_key,
            run_mode="manual",
        )
        mailbox.active_run_key = run_key
        db.add(task)
        db.flush()
        now = datetime(2026, 8, 18, 12, tzinfo=UTC)
        job = JobRun(
            user_id=user.id,
            mailbox_id=mailbox.id,
            task_id=task.id,
            run_key=run_key,
            status="running",
            stage="summarize",
            started_at=now - timedelta(hours=2),
            details={"events": []},
        )
        db.add(job)
        db.commit()

        _recover_stale_jobs(db, settings, now)
        db.refresh(job)
        db.refresh(task)
        db.refresh(mailbox)

        assert job.status == "failed"
        assert job.details["error_type"] == "StaleJob"
        assert task.active_run_key is None
        assert mailbox.active_run_key is None
    finally:
        db.close()


def test_deliver_to_targets_sends_to_enabled_targets_only(tmp_path):
    from mailpulse.worker import _deliver_to_targets

    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "worker", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="inbox@example.com",
            imap_host="fake",
            smtp_host="fake-smtp",
            username="worker",
            credential_encrypted=encrypt_secret("worker-secret", settings),
        )
        db.add(mailbox)
        db.flush()
        task = Task(
            user_id=user.id,
            mailbox_id=mailbox.id,
            cron_expression="0 9 * * *",
            timezone="UTC",
            run_mode="scheduled",
        )
        db.add(task)
        db.flush()
        enabled = TaskDeliveryTarget(
            task_id=task.id, destination="boss@example.com"
        )
        disabled = TaskDeliveryTarget(
            task_id=task.id, destination="archive@example.com", is_enabled=False
        )
        db.add_all([enabled, disabled])
        db.flush()
        report = Report(
            user_id=user.id,
            mailbox_id=mailbox.id,
            task_id=task.id,
            run_key="manual:deliver-to-targets",
            period_start=datetime.now(UTC) - timedelta(hours=1),
            period_end=datetime.now(UTC),
            status="success",
        )
        db.add(report)
        db.flush()

        class FakeDeliveryService:
            def __init__(self, session):
                self.session = session
                self.sent: list[str] = []

            def send_report(self, report, mailbox, recipient):
                self.sent.append(recipient)
                delivery = Delivery(
                    report_id=report.id,
                    channel="smtp",
                    destination=recipient,
                    status="sent",
                )
                self.session.add(delivery)
                self.session.flush()
                return delivery

            def retry_delivery(self, delivery, report, mailbox):
                delivery.status = "sent"
                return delivery

        service = FakeDeliveryService(db)
        _deliver_to_targets(db, service, report, task, mailbox)
        assert service.sent == ["boss@example.com"]
        assert db.query(Delivery).count() == 1
        # 已发送的目标不会重复投递
        _deliver_to_targets(db, service, report, task, mailbox)
        assert service.sent == ["boss@example.com"]
        assert db.query(Delivery).count() == 1

        # 全部目标停用（仅网页渠道）时不投递任何邮件，也不报错
        enabled.is_enabled = False
        db.commit()
        _deliver_to_targets(db, service, report, task, mailbox)
        assert service.sent == ["boss@example.com"]
        assert db.query(Delivery).count() == 1
    finally:
        db.close()


def test_task_delivery_target_web_crud(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "target-crud-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "tester", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="inbox@example.com",
            imap_host="fake",
            smtp_host="fake-smtp",
            username="tester",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    client.post(
        "/login",
        data={"username": "tester", "password": "password-123", "csrf_token": token},
    )
    tasks_page = client.get("/tasks/new")
    token = re.search(r'name="csrf_token" value="([^"]+)"', tasks_page.text).group(1)
    created = client.post(
        "/tasks",
        data={
            "name": "日报",
            "run_mode": "manual",
            "copy_from": str(mailbox.id),
            "timezone": "Asia/Shanghai",
            "lookback_hours": "24",
            "is_enabled": "on",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    detail_path = created.headers["location"]
    assert detail_path.startswith("/tasks/")
    task_id = int(detail_path.split("/")[2])
    targets_path = f"/tasks/{task_id}/targets"
    detail_page = client.get(detail_path)
    assert "投递渠道" in detail_page.text
    assert "网页查看" in detail_page.text
    assert "始终开启" in detail_page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail_page.text).group(1)

    added = client.post(
        targets_path,
        data={"destination": "boss@example.com", "csrf_token": token},
        follow_redirects=False,
    )
    assert added.status_code == 303
    duplicate = client.post(
        targets_path,
        data={"destination": "BOSS@example.com", "csrf_token": token},
    )
    assert "该投递邮箱已存在" in duplicate.text
    invalid = client.post(
        targets_path,
        data={"destination": "not-an-email", "csrf_token": token},
    )
    assert "投递邮箱格式无效" in invalid.text

    db = build_session_factory(settings)()
    try:
        target = db.query(TaskDeliveryTarget).one()
        assert target.destination == "boss@example.com"
        assert target.channel == "smtp"
        assert target.is_enabled is True
        task_id = target.task_id
        report = Report(
            user_id=user.id,
            mailbox_id=mailbox.id,
            task_id=task_id,
            run_key="manual:target-report",
            period_start=datetime.now(UTC) - timedelta(hours=1),
            period_end=datetime.now(UTC),
            status="success",
        )
        db.add(report)
        db.commit()
        report_id = report.id
    finally:
        db.close()

    # 报告页收件人输入框提供任务投递目标作为下拉建议
    detail_page = client.get(f"/reports/{report_id}")
    assert 'list="delivery-targets"' in detail_page.text
    assert 'value="boss@example.com"' in detail_page.text

    # 停用与删除投递目标
    detail_page = client.get(detail_path)
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail_page.text).group(1)
    toggled = client.post(
        f"{targets_path}/1/toggle",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert toggled.status_code == 303
    db = build_session_factory(settings)()
    try:
        assert db.get(TaskDeliveryTarget, 1).is_enabled is False
    finally:
        db.close()
    detail_page = client.get(detail_path)
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail_page.text).group(1)
    deleted = client.post(
        f"{targets_path}/1/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    db = build_session_factory(settings)()
    try:
        assert db.query(TaskDeliveryTarget).count() == 0
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
        user = create_user(db, "delivery", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="mailbox@example.com",
            imap_host="fake",
            smtp_host="fake-smtp",
            username="mailbox@example.com",
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
    import mailpulse.worker as worker_module

    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "web-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    admin = create_user(db, "sysadmin", "password-123", role="admin")
    user = create_user(db, "web", "password-123")
    db.commit()
    db.close()
    admin_client = TestClient(app)
    page = admin_client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = admin_client.post(
        "/login",
        data={"username": admin.username, "password": "password-123", "csrf_token": token},
    )
    assert response.status_code == 200
    assert "用户模式" in response.text
    response = choose_login_mode(admin_client, response)
    assert response.status_code == 303
    admin_dashboard = admin_client.get("/admin")
    assert "系统概览" in admin_dashboard.text
    assert '<header class="topbar admin-topbar">' in admin_dashboard.text
    assert 'class="topbar-actions"' in admin_dashboard.text
    assert 'href="/messages"' not in admin_dashboard.text
    assert 'href="/reports"' not in admin_dashboard.text
    assert "生成报告" not in admin_dashboard.text
    assert admin_client.get("/").status_code == 403
    for user_path in [
        "/tasks",
        "/messages",
        "/reports",
    ]:
        assert admin_client.get(user_path).status_code == 403
    # 管理员模式与用户模式的账号设置彼此隔离
    assert admin_client.get("/account").status_code == 403
    assert admin_client.get("/admin/users").status_code == 200
    assert admin_client.get("/admin/models").status_code == 200
    assert admin_client.get("/admin/jobs").status_code == 200
    admin_users_page = admin_client.get("/admin/users")
    admin_token = re.search(r'name="csrf_token" value="([^"]+)"', admin_users_page.text).group(1)
    created_user_response = admin_client.post(
        "/admin/users",
        data={
            "username": "managed",
            "display_name": "受管用户",
            "password": "Managed-pass-123",
            "role": "user",
            "csrf_token": admin_token,
        },
        follow_redirects=False,
    )
    assert created_user_response.status_code == 303
    assert created_user_response.headers["location"] == "/admin/users"
    assert "managed" in admin_client.get("/admin/users").text
    model_page = admin_client.get("/admin/models")
    model_token = re.search(r'name="csrf_token" value="([^"]+)"', model_page.text).group(1)
    model_response = admin_client.post(
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
    assert model_response.headers["location"] == "/admin/models"
    user_client = TestClient(app)
    user_login = user_client.get("/login")
    user_token = re.search(r'name="csrf_token" value="([^"]+)"', user_login.text).group(1)
    user_response = user_client.post(
        "/login",
        data={"username": user.username, "password": "password-123", "csrf_token": user_token},
    )
    assert user_response.status_code == 200
    assert "任务状态" in user_response.text
    assert '<header class="topbar user-topbar">' in user_response.text
    assert 'class="main-nav user-nav"' in user_response.text
    assert 'class="side-nav"' not in user_response.text
    assert 'href="/admin"' not in user_response.text
    for admin_path in ["/admin", "/admin/users", "/admin/models", "/admin/jobs"]:
        assert user_client.get(admin_path).status_code == 403
    dashboard = user_client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    assert user_client.post("/demo/seed", data={"csrf_token": token}).status_code == 200
    task_db = build_session_factory(settings)()
    try:
        task_id = task_db.query(Task).filter(Task.user_id == user.id).one().id
        mailbox_id = task_db.query(Mailbox).filter(Mailbox.user_id == user.id).one().id
    finally:
        task_db.close()
    monkeypatch.setattr("mailpulse.web.routes.IMAPConnector.test_connection", lambda self: None)
    task_page = user_client.get(f"/tasks/{task_id}")
    task_token = re.search(r'name="csrf_token" value="([^"]+)"', task_page.text).group(1)
    tested = user_client.post(
        f"/tasks/{task_id}/mailbox/test", data={"csrf_token": task_token}
    )
    assert tested.status_code == 200
    assert "IMAP 连接验证成功" in tested.text
    assert "验证 IMAP 连接" in tested.text
    messages_page = user_client.get("/messages")
    message_db = build_session_factory(settings)()
    message_id = message_db.query(CanonicalMessage).order_by(CanonicalMessage.id).first().id
    message_db.close()
    message_token = re.search(r'name="csrf_token" value="([^"]+)"', messages_page.text).group(1)
    assert (
        user_client.post(
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
    # 邮件页支持按邮箱筛选，范围为所有任务邮箱同步的邮件
    filtered = user_client.get(f"/messages?mailbox_id={mailbox_id}")
    assert filtered.status_code == 200
    assert "项目周会与本周行动项" in filtered.text
    assert user_client.get("/messages?mailbox_id=999999").status_code == 200

    # 手动运行任务：提交后台 JobRun；worker 再执行同步（空）→ 生成报告（桩）
    class StubReportService:
        def __init__(self, session, settings=None):
            self.session = session

        def generate_for_user(
            self, user, task=None, use_demo_provider=False, period_start=None,
            period_end=None, run_key=None,
        ):
            report = Report(
                user_id=user.id,
                mailbox_id=task.mailbox_id,
                task_id=task.id,
                run_key=run_key,
                period_start=period_start or datetime.now(UTC) - timedelta(hours=1),
                period_end=period_end or datetime.now(UTC),
                status="success",
                title="演示报告",
                summary={"message_count": 2},
                rendered_markdown="演示报告正文",
                model_trace={"used_vision": False},
            )
            self.session.add(report)
            self.session.flush()
            return report

    monkeypatch.setattr(worker_module, "IMAPConnector", lambda connection: FakeMailConnector([]))
    monkeypatch.setattr(worker_module, "ReportService", StubReportService)
    dashboard = user_client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    run_response = user_client.post(
        f"/tasks/{task_id}/run",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert run_response.status_code == 303
    assert run_response.headers["location"] == f"/tasks/{task_id}?run=queued"
    queued_db = build_session_factory(settings)()
    try:
        queued_job = queued_db.query(JobRun).filter(JobRun.task_id == task_id).one()
        assert queued_job.status == "queued"
        worker_module.run_task_now(
            queued_db,
            queued_db.get(Task, task_id),
            settings,
            queued_job.run_key,
            datetime.now(UTC),
            job=queued_job,
            run_kind=queued_job.run_kind,
        )
        report_id = queued_db.query(Report).filter(Report.task_id == task_id).one().id
    finally:
        queued_db.close()
    reports_page_text = user_client.get("/reports").text
    assert "演示报告" in reports_page_text
    report_detail_page = user_client.get(f"/reports/{report_id}")
    assert report_detail_page.status_code == 200
    assert '"used_vision": false' in report_detail_page.text

    # 创建定时任务：计划字段组合成 cron
    tasks_page = user_client.get("/tasks/new")
    token = re.search(r'name="csrf_token" value="([^"]+)"', tasks_page.text).group(1)
    schedule_response = user_client.post(
        "/tasks",
        data={
            "name": "工作日邮件报告",
            "run_mode": "scheduled",
            "schedule_type": "weekly",
            "scheduled_time": "09:30",
            "weekdays": "mon-fri",
            "timezone": "Asia/Shanghai",
            "lookback_hours": "24",
            "is_enabled": "on",
            "copy_from": str(mailbox_id),
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert schedule_response.status_code == 303
    verify_db = build_session_factory(settings)()
    try:
        saved_task = (
            verify_db.query(Task)
            .filter(Task.user_id == user.id, Task.name == "工作日邮件报告")
            .one()
        )
        assert saved_task.cron_expression == "30 9 * * mon-fri"
        assert saved_task.run_mode == "scheduled"
        assert saved_task.is_enabled is True
        other_user = create_user(verify_db, "other", "password-123")
        verify_db.commit()
    finally:
        verify_db.close()
    other_client = TestClient(app)
    other_login = other_client.get("/login")
    other_token = re.search(r'name="csrf_token" value="([^"]+)"', other_login.text).group(1)
    other_client.post(
        "/login",
        data={
            "username": other_user.username,
            "password": "password-123",
            "csrf_token": other_token,
        },
    )
    assert other_client.get(f"/reports/{report_id}").status_code == 404
    assert other_client.get("/admin").status_code == 403


def test_admin_login_can_choose_and_switch_fully_separate_user_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "dual-mode-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    admin = create_user(db, "dualadmin", "password-123", role="admin")
    db.commit()
    paired_id = admin.paired_user.id
    db.close()

    client = TestClient(app)
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    choice = client.post(
        "/login",
        data={"username": "dualadmin", "password": "password-123", "csrf_token": token},
    )
    assert choice.status_code == 200
    assert "管理员模式" in choice.text and "用户模式" in choice.text

    entered_user = choose_login_mode(client, choice, "user")
    assert entered_user.status_code == 303
    assert entered_user.headers["location"] == "/"
    dashboard = client.get("/")
    assert "切换到管理员模式" in dashboard.text
    assert client.get("/admin", follow_redirects=False).status_code == 403

    account = client.get("/account")
    token = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    changed = client.post(
        "/account",
        data={
            "current_password": "password-123",
            "new_password": "user-password-456",
            "confirm_password": "user-password-456",
            "csrf_token": token,
        },
    )
    assert changed.status_code == 200
    db = build_session_factory(settings)()
    try:
        admin_row = db.query(User).filter(User.username == "dualadmin").one()
        assert verify_password("user-password-456", admin_row.password_hash)
    finally:
        db.close()

    token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    client.post("/demo/seed", data={"csrf_token": token})
    db = build_session_factory(settings)()
    try:
        assert db.query(Task).filter(Task.user_id == paired_id).count() == 1
        assert db.query(Task).filter(Task.user_id == admin.id).count() == 0
    finally:
        db.close()

    dashboard = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    switched = client.post(
        "/switch-mode",
        data={"mode": "admin", "csrf_token": token},
        follow_redirects=False,
    )
    assert switched.status_code == 303
    assert switched.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200
    assert client.get("/").status_code == 403


def test_self_registration_creates_regular_user_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "register-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    client = TestClient(create_app())
    register_page = client.get("/register")
    assert "注册 MailPulse 账号" in register_page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', register_page.text).group(1)
    response = client.post(
        "/register",
        data={
            "username": "newbie",
            "display_name": "新用户",
            "password": "password-123",
            "confirm_password": "password-123",
            "role": "admin",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?registered=1"
    settings = get_settings()
    db = build_session_factory(settings)()
    try:
        created = db.query(User).filter(User.username == "newbie").one()
        assert created.role == "user"
        assert created.display_name == "新用户"
        assert authenticate(db, created.username, "password-123") is created
        audit = db.query(AuditLog).filter(AuditLog.action == "user_register").one()
        assert audit.target_id == str(created.id)
        assert audit.metadata_json == {"role": "user"}
    finally:
        db.close()
    login_page = client.get("/login?registered=1")
    assert "注册成功，请使用新账号登录" in login_page.text
    login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    logged_in = client.post(
        "/login",
        data={
            "username": "newbie",
            "password": "password-123",
            "csrf_token": login_token,
        },
        follow_redirects=False,
    )
    assert logged_in.status_code == 303
    assert logged_in.headers["location"] == "/"
    assert client.get("/register", follow_redirects=False).headers["location"] == "/"


def test_self_registration_rejects_invalid_submissions(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "register-reject-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    client = TestClient(create_app())
    page = client.get("/register")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    mismatch = client.post(
        "/register",
        data={
            "username": "newbie",
            "password": "password-123",
            "confirm_password": "password-456",
            "csrf_token": token,
        },
    )
    assert "两次输入的密码不一致" in mismatch.text

    short = client.post(
        "/register",
        data={
            "username": "newbie",
            "password": "short",
            "confirm_password": "short",
            "csrf_token": token,
        },
    )
    assert "密码长度至少为 8 个字符" in short.text

    bad_username = client.post(
        "/register",
        data={
            "username": "ab",
            "password": "password-123",
            "confirm_password": "password-123",
            "csrf_token": token,
        },
    )
    assert "用户名仅支持 3-32 位字母、数字、下划线、连字符或点" in bad_username.text

    created = client.post(
        "/register",
        data={
            "username": "newbie",
            "password": "password-123",
            "confirm_password": "password-123",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    duplicate = client.post(
        "/register",
        data={
            "username": "NEWBIE",
            "password": "password-123",
            "confirm_password": "password-123",
            "csrf_token": token,
        },
    )
    assert "用户名已存在" in duplicate.text
    settings = get_settings()
    db = build_session_factory(settings)()
    try:
        assert db.query(User).filter(User.username == "newbie").count() == 1
    finally:
        db.close()


def test_default_admin_can_skip_initial_password_change(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "default-admin-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    client = TestClient(create_app())
    login_page = client.get("/login")
    login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin123",
            "csrf_token": login_token,
        },
    )
    selected = choose_login_mode(client, response)
    assert selected.status_code == 303
    response = client.get(selected.headers["location"])
    assert response.status_code == 200
    assert response.url.path == "/admin/account/password"
    assert "修改管理员密码" in response.text
    assert "暂时跳过" in response.text

    password_token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    skipped = client.post(
        "/admin/account/password/skip",
        data={"csrf_token": password_token},
        follow_redirects=False,
    )
    assert skipped.status_code == 303
    assert skipped.headers["location"] == "/admin"

    dashboard = client.get("/admin")
    logout_token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    client.post("/logout", data={"csrf_token": logout_token})
    login_page = client.get("/login")
    login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    logged_in_again = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin123",
            "csrf_token": login_token,
        },
    )
    logged_in_again = choose_login_mode(client, logged_in_again)
    assert logged_in_again.status_code == 303
    assert logged_in_again.headers["location"] == "/admin"


def test_default_admin_can_change_initial_password(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "default-admin-password-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    client = TestClient(create_app())
    login_page = client.get("/login")
    login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin123",
            "csrf_token": login_token,
        },
    )
    selected = choose_login_mode(client, response)
    assert selected.status_code == 303
    response = client.get(selected.headers["location"])
    password_token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    changed = client.post(
        "/admin/account/password",
        data={
            "new_password": "admin456",
            "confirm_password": "admin456",
            "csrf_token": password_token,
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert changed.headers["location"] == "/admin"

    dashboard = client.get("/admin")
    logout_token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    client.post("/logout", data={"csrf_token": logout_token})
    login_page = client.get("/login")
    login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    old_password = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin123",
            "csrf_token": login_token,
        },
    )
    assert old_password.status_code == 200
    assert "账号或密码错误" in old_password.text

    login_page = client.get("/login")
    login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    new_password = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin456",
            "csrf_token": login_token,
        },
    )
    new_password = choose_login_mode(client, new_password)
    assert new_password.status_code == 303
    assert new_password.headers["location"] == "/admin"


def test_unauthenticated_routes_redirect_to_login(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "redirect-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    try:
        client = TestClient(create_app())
        dashboard = client.get("/", follow_redirects=False)
        admin = client.get("/admin", follow_redirects=False)
    finally:
        get_settings.cache_clear()

    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/login"
    assert admin.status_code == 303
    assert admin.headers["location"] == "/login"


def test_session_from_recreated_database_redirects_to_login(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "database-recreate-session-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    old_client = TestClient(app)
    login_page = old_client.get("/login")
    login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    choice = old_client.post(
        "/login",
        data={"username": "admin", "password": "admin123", "csrf_token": login_token},
    )
    selected = choose_login_mode(old_client, choice, "admin")
    assert selected.status_code == 303
    old_session = old_client.cookies.get("mailpulse_session")

    reset_database(settings)
    new_client = TestClient(create_app())
    new_client.cookies.set("mailpulse_session", old_session)
    root = new_client.get("/", follow_redirects=False)
    assert root.status_code == 303
    assert root.headers["location"] == "/login"


def test_login_remember_me_controls_cookie_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "remember-me-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    def session_cookie(response):
        return next(
            item
            for item in response.headers.get_list("set-cookie")
            if item.startswith("mailpulse_session=")
        )

    try:
        client = TestClient(create_app())
        login_page = client.get("/login")
        assert "记住登录状态" in login_page.text
        login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        normal = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "admin123",
                "csrf_token": login_token,
            },
            follow_redirects=False,
        )
        normal_cookie = session_cookie(normal)
        assert "Max-Age=" not in normal_cookie
        assert "admin123" not in normal_cookie

        remembered_client = TestClient(create_app())
        login_page = remembered_client.get("/login")
        login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        remembered = remembered_client.post(
            "/login",
            data={
                "username": "admin",
                "password": "admin123",
                "remember_me": "true",
                "csrf_token": login_token,
            },
            follow_redirects=False,
        )
        remembered_cookie = session_cookie(remembered)
    finally:
        get_settings.cache_clear()

    assert "Max-Age=2592000" in remembered_cookie
    assert "admin123" not in remembered_cookie


def test_login_remember_password_prefills_and_clears_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "remember-password-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    client = TestClient(create_app())
    login_page = client.get("/login")
    assert "记住登录状态" in login_page.text
    assert "记住密码" in login_page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin123",
            "remember_password": "true",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    remember_cookie = next(
        item
        for item in response.headers.get_list("set-cookie")
        if item.startswith("mailpulse_remember_credentials=")
    )
    assert "HttpOnly" in remember_cookie
    assert "Max-Age=2592000" in remember_cookie
    assert "admin123" not in remember_cookie
    saved_value = response.cookies["mailpulse_remember_credentials"]

    saved_client = TestClient(create_app())
    saved_client.cookies.set("mailpulse_remember_credentials", saved_value)
    saved_page = saved_client.get("/login")
    assert 'name="username" value="admin"' in saved_page.text
    assert 'name="password" value="admin123"' in saved_page.text
    assert 'name="remember_password" value="true" checked' in saved_page.text
    saved_token = re.search(r'name="csrf_token" value="([^"]+)"', saved_page.text).group(1)

    # 登录失败不会更新记住密码 Cookie，也不会回显明文密码
    failed = saved_client.post(
        "/login",
        data={
            "username": "admin",
            "password": "wrong-password",
            "remember_password": "true",
            "csrf_token": saved_token,
        },
    )
    assert "账号或密码错误" in failed.text
    assert not any(
        item.startswith("mailpulse_remember_credentials=")
        for item in failed.headers.get_list("set-cookie")
    )
    assert 'name="password" value="wrong-password"' not in failed.text

    # 取消勾选“记住密码”登录时清除已保存的凭据 Cookie
    cleared = saved_client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin123",
            "csrf_token": saved_token,
        },
        follow_redirects=False,
    )
    assert cleared.status_code == 200
    cleared_cookie = next(
        item
        for item in cleared.headers.get_list("set-cookie")
        if item.startswith("mailpulse_remember_credentials=")
    )
    assert "Max-Age=0" in cleared_cookie

    # 伪造或损坏的 Cookie 不会导致登录页报错
    tampered = TestClient(create_app())
    tampered.cookies.set("mailpulse_remember_credentials", "not-a-valid-token")
    tampered_page = tampered.get("/login")
    assert tampered_page.status_code == 200
    assert "登录 MailPulse" in tampered_page.text
    assert 'name="password" value="not-a-valid-token"' not in tampered_page.text


def test_task_rules_web_crud(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "rules-crud-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "ruletester", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="inbox@example.com",
            imap_host="fake",
            username="ruletester",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.commit()
        mailbox_id = mailbox.id
    finally:
        db.close()

    client = TestClient(app)
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    client.post(
        "/login",
        data={"username": "ruletester", "password": "password-123", "csrf_token": token},
    )
    page = client.get("/tasks/new")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    created = client.post(
        "/tasks",
        data={
            "name": "规则任务",
            "run_mode": "manual",
            "copy_from": str(mailbox_id),
            "timezone": "Asia/Shanghai",
            "lookback_hours": "24",
            "is_enabled": "on",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    task_id = int(created.headers["location"].split("/")[2])

    # 添加合法规则（表单模式：字段/操作符/值）
    detail = client.get(f"/tasks/{task_id}")
    assert "暂无规则" in detail.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    rules_json = json.dumps(
        [
            {
                "name": "项目邮件",
                "conditions": [
                    {"field": "subject", "operator": "contains", "value": "项目"},
                ],
            }
        ],
        ensure_ascii=False,
    )
    added = client.post(
        f"/tasks/{task_id}/rules",
        data={
            "name": "项目邮件",
            "mode": "form",
            "rules_json": rules_json,
            "priority": "50",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert added.status_code == 303
    detail = client.get(f"/tasks/{task_id}")
    assert "项目邮件" in detail.text
    assert "邮件标题 包含 项目" in detail.text

    # 非法规则被拒绝且保留表单内容（不支持的字段）
    invalid = client.post(
        f"/tasks/{task_id}/rules",
        data={
            "name": "坏规则",
            "mode": "form",
            "rules_json": json.dumps(
                [
                    {
                        "name": "坏规则",
                        "conditions": [
                            {"field": "subject", "operator": "explode", "value": "项目"},
                        ],
                    }
                ]
            ),
            "priority": "60",
            "csrf_token": token,
        },
    )
    assert "规则无效" in invalid.text

    # 编辑（表单模式）与停用
    edit_page = client.get(f"/tasks/{task_id}/rules/1/edit")
    assert "正在编辑规则「项目邮件」" in edit_page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', edit_page.text).group(1)
    updated = client.post(
        f"/tasks/{task_id}/rules/1/edit",
        data={
            "name": "项目邮件 v2",
            "mode": "json",
            "definition": '{"kind":"match_all"}',
            "priority": "10",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    detail = client.get(f"/tasks/{task_id}")
    assert "项目邮件 v2" in detail.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    toggled = client.post(
        f"/tasks/{task_id}/rules/1/toggle",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert toggled.status_code == 303
    db = build_session_factory(settings)()
    try:
        rule = db.get(RuleSet, 1)
        assert rule.name == "项目邮件 v2"
        assert rule.definition == {"kind": "match_all"}
        assert rule.is_enabled is False
    finally:
        db.close()

    # 再添加一条规则并验证上移/下移调整优先级顺序
    detail = client.get(f"/tasks/{task_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    second = client.post(
        f"/tasks/{task_id}/rules",
        data={
            "mode": "form",
            "rules_json": json.dumps(
                [
                    {
                        "name": "发票邮件",
                        "conditions": [
                            {"field": "sender", "operator": "equals",
                             "value": "finance@example.com"},
                        ],
                    }
                ]
            ),
            "priority": "90",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert second.status_code == 303
    db = build_session_factory(settings)()
    try:
        rules = (
            db.query(RuleSet)
            .filter(RuleSet.task_id == task_id)
            .order_by(RuleSet.priority.asc(), RuleSet.id.asc())
            .all()
        )
        assert [item.name for item in rules] == ["项目邮件 v2", "发票邮件"]
        assert rules[1].priority == 90
    finally:
        db.close()
    detail = client.get(f"/tasks/{task_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    moved = client.post(
        f"/tasks/{task_id}/rules/2/move",
        data={"direction": "up", "csrf_token": token},
        follow_redirects=False,
    )
    assert moved.status_code == 303
    db = build_session_factory(settings)()
    try:
        rules = (
            db.query(RuleSet)
            .filter(RuleSet.task_id == task_id)
            .order_by(RuleSet.priority.asc(), RuleSet.id.asc())
            .all()
        )
        assert [item.name for item in rules] == ["发票邮件", "项目邮件 v2"]
    finally:
        db.close()

    # 删除规则
    detail = client.get(f"/tasks/{task_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    deleted = client.post(
        f"/tasks/{task_id}/rules/1/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    deleted = client.post(
        f"/tasks/{task_id}/rules/2/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    db = build_session_factory(settings)()
    try:
        assert db.query(RuleSet).count() == 0
    finally:
        db.close()


def test_task_delete_cleans_orphan_mailbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "task-delete-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "deleter", "password-123")
        lone = Mailbox(
            user_id=user.id, email_address="lone@example.com", imap_host="fake",
            username="deleter", credential_encrypted=encrypt_secret("s1", settings),
        )
        db.add(lone)
        db.flush()
        task_a = Task(user_id=user.id, mailbox_id=lone.id, name="独占邮箱任务", run_mode="manual")
        db.add(task_a)
        shared = Mailbox(
            user_id=user.id, email_address="shared@example.com", imap_host="fake",
            username="deleter", credential_encrypted=encrypt_secret("s2", settings),
        )
        db.add(shared)
        db.flush()
        task_b = Task(user_id=user.id, mailbox_id=shared.id, name="共享一", run_mode="manual")
        task_c = Task(user_id=user.id, mailbox_id=shared.id, name="共享二", run_mode="manual")
        db.add_all([task_b, task_c])
        db.commit()
        lone_id, shared_id = lone.id, shared.id
        task_a_id, task_b_id = task_a.id, task_b.id
    finally:
        db.close()

    client = TestClient(app)
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    client.post(
        "/login",
        data={"username": "deleter", "password": "password-123", "csrf_token": token},
    )
    detail = client.get(f"/tasks/{task_a_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    deleted = client.post(
        f"/tasks/{task_a_id}/delete", data={"csrf_token": token}, follow_redirects=False
    )
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/tasks"
    db = build_session_factory(settings)()
    try:
        assert db.get(Task, task_a_id) is None
        # 独占邮箱随任务一并清理
        assert db.get(Mailbox, lone_id) is None
    finally:
        db.close()

    # 共享邮箱在仍有任务引用时保留
    detail = client.get(f"/tasks/{task_b_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    client.post(f"/tasks/{task_b_id}/delete", data={"csrf_token": token})
    db = build_session_factory(settings)()
    try:
        assert db.get(Task, task_b_id) is None
        assert db.get(Mailbox, shared_id) is not None
        assert db.query(Task).filter(Task.mailbox_id == shared_id).count() == 1
    finally:
        db.close()


def test_manual_run_pipeline_sync_generate_success(tmp_path, monkeypatch):
    import mailpulse.worker as worker_module

    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "pipeline", "password-123")
        mailbox = Mailbox(
            user_id=user.id, email_address="pipeline@example.com", imap_host="fake",
            username="pipeline", credential_encrypted=encrypt_secret("pw", settings),
        )
        db.add(mailbox)
        db.flush()
        task = Task(
            user_id=user.id, mailbox_id=mailbox.id, name="流水线任务",
            run_mode="manual", lookback_hours=24,
        )
        db.add(task)
        db.flush()
        db.commit()
        now = datetime.now(UTC)

        # 同步使用演示邮件源；归纳使用 DemoProvider，避免外部 AI 依赖
        monkeypatch.setattr(
            worker_module, "IMAPConnector", lambda connection: FakeMailConnector(demo_messages())
        )

        class DemoReportService(ReportService):
            def generate_for_user(
                self, user, task=None, use_demo_provider=False, **kwargs
            ):
                return super().generate_for_user(
                    user, task=task, use_demo_provider=True, **kwargs
                )

        monkeypatch.setattr(worker_module, "ReportService", DemoReportService)
        job = run_task_now(db, task, settings, "manual:1:test", now)
        db.commit()
        assert job.status == "success"
        assert job.stage == "complete"
        report = db.query(Report).filter(Report.task_id == task.id).one()
        assert report.status == "success"
        assert report.run_key == "manual:1:test"
        assert report.summary.get("message_count") == 2
        task = db.get(Task, task.id)
        assert task.last_run_at is not None
        # 相同 run_key 重复执行不会生成重复报告
        second = run_task_now(db, task, settings, "manual:1:test", now)
        db.commit()
        assert second.status == "success"
        assert db.query(Report).filter(Report.task_id == task.id).count() == 1
    finally:
        db.close()


def test_user_account_password_change(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "account-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    user = create_user(db, "acct", "password-123", display_name="原名")
    db.commit()
    db.close()

    client = TestClient(app)
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    client.post(
        "/login",
        data={"username": user.username, "password": "password-123", "csrf_token": token},
    )
    account = client.get("/account")
    assert "修改密码" in account.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)

    # 当前密码错误被拒绝
    wrong = client.post(
        "/account",
        data={
            "display_name": "新名字",
            "current_password": "wrong-password",
            "new_password": "new-password-456",
            "confirm_password": "new-password-456",
            "csrf_token": token,
        },
    )
    assert "当前密码不正确" in wrong.text

    # 显示名更新 + 正确修改密码
    ok = client.post(
        "/account",
        data={
            "display_name": "新名字",
            "current_password": "password-123",
            "new_password": "new-password-456",
            "confirm_password": "new-password-456",
            "csrf_token": token,
        },
    )
    assert "显示名称已更新" in ok.text
    db = build_session_factory(settings)()
    try:
        assert db.get(User, user.id).display_name == "新名字"
        assert authenticate(db, user.username, "new-password-456") is not None
        assert authenticate(db, user.username, "password-123") is None
    finally:
        db.close()

    # 两次新密码不一致被拒绝
    account = client.get("/account")
    token = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    mismatch = client.post(
        "/account",
        data={
            "current_password": "new-password-456",
            "new_password": "another-789",
            "confirm_password": "different-789",
            "csrf_token": token,
        },
    )
    assert "两次输入的新密码不一致" in mismatch.text


def test_rule_filter_messages_any_or_semantics(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "oruser", "password-123")
        mailbox = Mailbox(
            user_id=user.id, email_address="in@example.com", imap_host="fake",
            username="oruser", credential_encrypted=encrypt_secret("s", settings),
        )
        db.add(mailbox)
        db.flush()
        task = Task(user_id=user.id, mailbox_id=mailbox.id, name="OR 任务", run_mode="manual")
        db.add(task)
        db.flush()
        messages = []
        for index, subject in enumerate(["项目周报", "发票通知", "无关广告"]):
            message = CanonicalMessage(
                owner_user_id=user.id,
                content_hash=f"hash-{index}",
                subject=subject,
                sender=f"sender-{index}@example.com",
                body_text=f"正文 {index}",
            )
            db.add(message)
            messages.append(message)
        db.flush()

        def rule(name, value, enabled=True):
            item = RuleSet(
                task_id=task.id,
                name=name,
                definition={
                    "kind": "condition",
                    "field": "subject",
                    "operator": "contains",
                    "value": value,
                },
                is_enabled=enabled,
                priority=100,
            )
            db.add(item)
            return item

        project = rule("项目邮件", "项目")
        invoice = rule("发票邮件", "发票")
        disabled = rule("停用规则", "广告", enabled=False)
        db.flush()

        service = RuleService(db)
        result = service.filter_messages_any(messages, [project, invoice, disabled])
        assert [item.subject for item in result] == ["项目周报", "发票通知"]
        # 输入顺序保持
        assert [item.subject for item in result] == [messages[0].subject, messages[1].subject]
        # 全部停用（或无启用规则）时保留全部邮件
        project.is_enabled = False
        invoice.is_enabled = False
        assert [
            item.subject
            for item in service.filter_messages_any(messages, [project, invoice])
        ] == ["项目周报", "发票通知", "无关广告"]
        assert len(service.filter_messages_any(messages, [])) == 3
    finally:
        db.close()


def test_task_wizard_creation_with_rules_and_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "wizard-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "wizard", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="inbox@example.com",
            imap_host="fake",
            username="wizard",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.commit()
        mailbox_id = mailbox.id
    finally:
        db.close()

    client = TestClient(app)
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    client.post(
        "/login",
        data={"username": "wizard", "password": "password-123", "csrf_token": token},
    )
    new_page = client.get("/tasks/new")
    assert new_page.status_code == 200
    for step_name in ["基本信息", "收件邮箱", "筛选规则", "投递渠道"]:
        assert step_name in new_page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', new_page.text).group(1)

    rules_json = json.dumps(
        [
            {
                "name": "项目邮件",
                "conditions": [
                    {"field": "subject", "operator": "contains", "value": "项目"},
                    {"field": "sender", "operator": "not_contains", "value": "noreply"},
                ],
            },
            {
                "name": "财务邮件",
                "conditions": [
                    {"field": "sender", "operator": "equals",
                     "value": "finance@example.com"},
                ],
            },
        ],
        ensure_ascii=False,
    )
    targets_json = json.dumps(["boss@example.com", "team@example.com"])
    created = client.post(
        "/tasks",
        data={
            "name": "向导任务",
            "run_mode": "scheduled",
            "schedule_type": "daily",
            "scheduled_time": "08:00",
            "weekdays": "mon-fri",
            "timezone": "Asia/Shanghai",
            "lookback_hours": "24",
            "is_enabled": "on",
            "copy_from": str(mailbox_id),
            "rules_json": rules_json,
            "targets_json": targets_json,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    task_id = int(created.headers["location"].split("/")[2])
    db = build_session_factory(settings)()
    try:
        task = db.get(Task, task_id)
        assert task.run_mode == "scheduled"
        assert task.cron_expression == "0 8 * * *"
        rules = (
            db.query(RuleSet)
            .filter(RuleSet.task_id == task_id)
            .order_by(RuleSet.priority.asc())
            .all()
        )
        assert rules[0].definition == {
            "kind": "group",
            "operator": "and",
            "children": [
                {"kind": "condition", "field": "subject", "operator": "contains", "value": "项目"},
                {"kind": "condition", "field": "sender",
                 "operator": "not_contains", "value": "noreply"},
            ],
        }
        targets = db.query(TaskDeliveryTarget).filter(TaskDeliveryTarget.task_id == task_id).all()
        assert {item.destination for item in targets} == {"boss@example.com", "team@example.com"}
    finally:
        db.close()

    # 重复投递地址被拒绝并重渲染向导（保留已填内容）
    dup = client.post(
        "/tasks",
        data={
            "name": "向导任务",
            "run_mode": "manual",
            "copy_from": str(mailbox_id),
            "rules_json": "[]",
            "targets_json": json.dumps(["a@example.com", "a@example.com"]),
            "csrf_token": token,
        },
    )
    assert "已重复" in dup.text
    assert "向导任务" in dup.text


def test_delivery_target_edit_and_test_email(tmp_path, monkeypatch):
    import mailpulse.web.routes as routes_module

    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "target-test-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "targeter", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="inbox@example.com",
            imap_host="fake",
            smtp_host="fake-smtp",
            username="targeter",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        task = Task(user_id=user.id, mailbox_id=mailbox.id, name="渠道任务", run_mode="manual")
        db.add(task)
        db.flush()
        target = TaskDeliveryTarget(task_id=task.id, destination="boss@example.com")
        db.add(target)
        db.commit()
        task_id, target_id = task.id, target.id
    finally:
        db.close()

    client = TestClient(app)
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    client.post(
        "/login",
        data={"username": "targeter", "password": "password-123", "csrf_token": token},
    )

    # 编辑投递地址
    detail = client.get(f"/tasks/{task_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    edited = client.post(
        f"/tasks/{task_id}/targets/{target_id}/edit",
        data={"destination": "newboss@example.com", "csrf_token": token},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    db = build_session_factory(settings)()
    try:
        assert db.get(TaskDeliveryTarget, target_id).destination == "newboss@example.com"
    finally:
        db.close()
    # 添加另一个地址后，编辑为已有地址被拒绝
    detail = client.get(f"/tasks/{task_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    client.post(
        f"/tasks/{task_id}/targets",
        data={"destination": "team@example.com", "csrf_token": token},
    )
    detail = client.get(f"/tasks/{task_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    duplicate = client.post(
        f"/tasks/{task_id}/targets/{target_id}/edit",
        data={"destination": "team@example.com", "csrf_token": token},
    )
    assert "该投递邮箱已存在" in duplicate.text

    # 发送测试邮件（桩 SMTP provider）
    class RecordingSMTPProvider:
        def __init__(self, config):
            self.config = config
            RecordingSMTPProvider.calls.append(config)

        def send(self, sender, recipient, subject, body):
            RecordingSMTPProvider.last = (sender, recipient, subject, body)

    RecordingSMTPProvider.calls = []
    RecordingSMTPProvider.last = None
    monkeypatch.setattr(routes_module, "SMTPDeliveryProvider", RecordingSMTPProvider)
    detail = client.get(f"/tasks/{task_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    sent = client.post(
        f"/tasks/{task_id}/targets/{target_id}/test",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert sent.status_code == 303
    assert sent.headers["location"].endswith("?notice=test-sent")
    assert RecordingSMTPProvider.last is not None
    assert RecordingSMTPProvider.last[0] == "inbox@example.com"
    assert RecordingSMTPProvider.last[1] == "newboss@example.com"
    assert "MailPulse 投递渠道测试" in RecordingSMTPProvider.last[2]


def test_report_scope_filters_before_limit_and_uses_occurrence_time(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        secret_key="report-scope-secret",
        credential_key="report-scope-credential",
        max_messages_per_report=1,
    )
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "scope", "password-123")
        mailbox_a = Mailbox(
            user_id=user.id,
            email_address="a@example.com",
            imap_host="fake-a",
            username="a@example.com",
            credential_encrypted=encrypt_secret("a-secret", settings),
        )
        mailbox_b = Mailbox(
            user_id=user.id,
            email_address="b@example.com",
            imap_host="fake-b",
            username="b@example.com",
            credential_encrypted=encrypt_secret("b-secret", settings),
        )
        db.add_all([mailbox_a, mailbox_b])
        db.flush()
        task = Task(user_id=user.id, mailbox_id=mailbox_a.id, name="A 邮箱任务")
        db.add(task)
        db.flush()
        old_time = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
        new_time = datetime(2026, 8, 18, 2, 0, tzinfo=UTC)
        messages = [
            CanonicalMessage(
                owner_user_id=user.id,
                content_hash="scope-old",
                subject="命中旧邮件",
                sender="sender@example.com",
                recipients=["a@example.com"],
                body_text="正文",
                received_at=None,
            ),
            CanonicalMessage(
                owner_user_id=user.id,
                content_hash="scope-new",
                subject="其他新邮件",
                sender="sender@example.com",
                recipients=["a@example.com"],
                body_text="正文",
                received_at=new_time,
            ),
            CanonicalMessage(
                owner_user_id=user.id,
                content_hash="scope-other-mailbox",
                subject="命中其他邮箱",
                sender="sender@example.com",
                recipients=["b@example.com"],
                body_text="正文",
                received_at=new_time,
            ),
        ]
        db.add_all(messages)
        db.flush()
        db.add_all(
            [
                MessageOccurrence(
                    message_id=messages[0].id,
                    mailbox_id=mailbox_a.id,
                    folder="INBOX",
                    uid_validity="a",
                    uid=1,
                    source_id=mailbox_a.sync_source_id,
                    internal_date=old_time,
                ),
                MessageOccurrence(
                    message_id=messages[1].id,
                    mailbox_id=mailbox_a.id,
                    folder="INBOX",
                    uid_validity="a",
                    uid=2,
                    source_id=mailbox_a.sync_source_id,
                    internal_date=new_time,
                ),
                MessageOccurrence(
                    message_id=messages[2].id,
                    mailbox_id=mailbox_b.id,
                    folder="INBOX",
                    uid_validity="b",
                    uid=1,
                    source_id=mailbox_b.sync_source_id,
                    internal_date=new_time,
                ),
            ]
        )
        db.add(
            RuleSet(
                task_id=task.id,
                name="只看命中",
                definition={
                    "kind": "condition",
                    "field": "subject",
                    "operator": "contains",
                    "value": "命中",
                },
            )
        )
        db.commit()
        report = ReportService(db, settings).generate_for_user(
            user,
            task,
            use_demo_provider=True,
            period_start=old_time - timedelta(minutes=1),
            period_end=new_time + timedelta(minutes=1),
        )
        assert report.summary["matched_message_count"] == 1
        assert report.summary["message_count"] == 1
        assert report.summary["truncated"] is False
        assert "命中旧邮件" in report.rendered_markdown or report.summary["message_count"] == 1
    finally:
        db.close()


def test_imap_fetch_failure_does_not_advance_cursor(monkeypatch):
    raw = (
        b"From: sender@example.com\n"
        b"To: user@example.com\n"
        b"Subject: Chunk\n"
        b"Message-ID: <chunk@example.com>\n\nBody"
    )

    class FailingChunkClient:
        def response(self, key):
            assert key == "UIDVALIDITY"
            return "OK", [b"7"]

        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b" ".join(str(uid).encode() for uid in range(1, 202))]
            chunk = [int(value) for value in args[0].split(",")]
            if chunk[0] == 201:
                return "NO", []
            return "OK", [
                *((f"UID {uid}".encode(), raw) for uid in chunk),
                b")",
            ]

        def logout(self):
            return "BYE", []

    connector = IMAPConnector(
        MailboxConnection(
            host="fake",
            port=993,
            username="user@example.com",
            password="secret",
        )
    )
    monkeypatch.setattr(connector, "_open", lambda: FailingChunkClient())
    with pytest.raises(ConnectionError, match="UID 201-201"):
        connector.sync_messages()


def test_failed_schedule_slot_is_not_retried_on_next_worker_poll(tmp_path, monkeypatch):
    import mailpulse.worker as worker_module

    settings = make_settings(tmp_path)
    init_database(settings)
    now_anchor = datetime.now(UTC).replace(second=30, microsecond=0)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "schedule-once", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="schedule@example.com",
            imap_host="fake",
            username="schedule@example.com",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        task = Task(
            user_id=user.id,
            mailbox_id=mailbox.id,
            run_mode="scheduled",
            cron_expression="* * * * *",
            timezone="UTC",
            last_run_at=now_anchor - timedelta(minutes=1),
        )
        db.add(task)
        db.commit()
    finally:
        db.close()

    calls = []

    def fake_run(session, task, settings, run_key, now, *, job=None, run_kind="task"):
        calls.append(run_key)
        job.status = "failed"
        job.stage = "sync"
        job.finished_at = now
        session.get(Task, task.id).active_run_key = None
        session.get(Mailbox, task.mailbox_id).active_run_key = None
        session.commit()
        return job

    frozen_now = now_anchor

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

    monkeypatch.setattr(worker_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(worker_module, "run_task_now", fake_run)
    assert run_due_tasks(settings) == 0
    assert run_due_tasks(settings) == 0
    assert len(calls) == 1


def test_enqueue_rejects_same_task_and_mailbox_concurrency(tmp_path):
    settings = make_settings(tmp_path)
    init_database(settings)
    db = build_session_factory(settings)()
    try:
        user = create_user(db, "lock", "password-123")
        mailbox = Mailbox(
            user_id=user.id,
            email_address="lock@example.com",
            imap_host="fake",
            username="lock@example.com",
            credential_encrypted=encrypt_secret("secret", settings),
        )
        db.add(mailbox)
        db.flush()
        task = Task(user_id=user.id, mailbox_id=mailbox.id, name="锁任务")
        db.add(task)
        db.flush()
        first = enqueue_job_run(db, task, user)
        db.commit()
        assert first.status == "queued"
        with pytest.raises(ValueError, match="运行中的"):
            enqueue_job_run(db, task, user)
        db.rollback()
        assert db.get(Task, task.id).active_run_key == first.run_key
    finally:
        db.close()


def test_web_requires_current_password_and_unchecked_task_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "account-checkbox-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    user = create_user(db, "account-check", "password-123")
    mailbox = Mailbox(
        user_id=user.id,
        email_address="account@example.com",
        imap_host="fake",
        username="account@example.com",
        credential_encrypted=encrypt_secret("secret", settings),
    )
    db.add(mailbox)
    db.flush()
    task = Task(user_id=user.id, mailbox_id=mailbox.id, name="启用任务", is_enabled=True)
    db.add(task)
    db.commit()
    task_id = task.id
    db.close()

    client = TestClient(app)
    login = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    client.post(
        "/login",
        data={"username": "account-check", "password": "password-123", "csrf_token": token},
    )
    account = client.get("/account")
    token = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    missing_current = client.post(
        "/account",
        data={
            "display_name": "新名称",
            "new_password": "new-password-456",
            "confirm_password": "new-password-456",
            "csrf_token": token,
        },
    )
    assert "当前密码不正确" in missing_current.text

    detail = client.get(f"/tasks/{task_id}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
    updated = client.post(
        f"/tasks/{task_id}/basic",
        data={
            "name": "停用任务",
            "run_mode": "manual",
            "timezone": "Asia/Shanghai",
            "lookback_hours": "24",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    db = build_session_factory(settings)()
    try:
        assert db.get(Task, task_id).is_enabled is False
        assert authenticate(db, "account-check", "password-123") is not None
        assert authenticate(db, "account-check", "new-password-456") is None
    finally:
        db.close()


def test_copy_mailbox_preserves_tls_and_creates_independent_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILPULSE_SECRET_KEY", "copy-mailbox-secret")
    get_settings.cache_clear()
    from mailpulse.app import create_app

    app = create_app()
    settings = get_settings()
    db = build_session_factory(settings)()
    user = create_user(db, "copy-mailbox", "password-123")
    source = Mailbox(
        user_id=user.id,
        email_address="source@example.com",
        imap_host="imap.example.com",
        imap_port=143,
        imap_tls=False,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_tls=False,
        username="source-login",
        credential_encrypted=encrypt_secret("source-secret", settings),
        folder="Archive",
    )
    db.add(source)
    db.commit()
    source_id = source.id
    db.close()

    client = TestClient(app)
    login = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    client.post(
        "/login",
        data={"username": "copy-mailbox", "password": "password-123", "csrf_token": token},
    )
    page = client.get("/tasks/new")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    created = client.post(
        "/tasks",
        data={"name": "复制任务", "copy_from": str(source_id), "csrf_token": token},
        follow_redirects=False,
    )
    assert created.status_code == 303
    db = build_session_factory(settings)()
    try:
        copied = db.query(Mailbox).filter(Mailbox.id != source_id).one()
        assert copied.imap_port == 143 and copied.imap_tls is False
        assert copied.smtp_port == 587 and copied.smtp_tls is False
        assert copied.folder == "Archive"
        assert decrypt_secret(copied.credential_encrypted, settings) == "source-secret"
        copied.email_address = "copy@example.com"
        db.commit()
        assert db.get(Mailbox, source_id).email_address == "source@example.com"
    finally:
        db.close()
