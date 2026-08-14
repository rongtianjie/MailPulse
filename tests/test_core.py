from __future__ import annotations

import base64
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mailpulse.ai.orchestrator import AIOrchestrator, ModelRouter
from mailpulse.ai.types import GenerationRequest, GenerationResult
from mailpulse.attachments.converter import MarkItDownAttachmentConverter
from mailpulse.auth import create_user
from mailpulse.config import Settings, get_settings
from mailpulse.db import build_session_factory, init_database
from mailpulse.filtering import RuleEvaluator, RuleValidationError
from mailpulse.mail.connectors import FakeMailConnector
from mailpulse.mail.sync import MailSyncService
from mailpulse.mail.types import RawAttachment, RawMessage
from mailpulse.models import Attachment, Mailbox
from mailpulse.security import decrypt_secret, encrypt_secret, verify_password


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


class RecordingProvider:
    def __init__(self, name: str, response: dict):
        self.name = name
        self.response = response
        self.roles: list[str] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.roles.append(request.role)
        return GenerationResult(str(self.response), self.response, self.name)


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
    assert client.post("/demo/seed", data={}).status_code == 403
    dashboard = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    assert client.post("/demo/seed", data={"csrf_token": token}).status_code == 200
    dashboard = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
    report_response = client.post(
        "/reports/generate",
        data={"use_demo_provider": "true", "csrf_token": token},
    )
    assert report_response.status_code == 200
    assert "演示报告" in client.get("/reports").text
