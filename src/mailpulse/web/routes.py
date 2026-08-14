from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import authenticate, create_user
from ..config import get_settings
from ..delivery import ReportDeliveryService
from ..demo import seed_demo
from ..mail.connectors import IMAPConnector
from ..mail.sync import MailSyncService
from ..mail.types import MailboxConnection
from ..models import (
    AIProviderProfile,
    AuditLog,
    CanonicalMessage,
    Delivery,
    JobRun,
    Mailbox,
    ModelBinding,
    Report,
    RuleSet,
    Schedule,
    User,
)
from ..report_service import ReportService
from ..rules import MATCH_ALL, RuleService
from ..search import SearchService
from ..security import decrypt_secret, encrypt_secret
from ..worker import build_cron_expression, validate_schedule
from .csrf import get_csrf_token, validate_csrf
from .deps import admin_user, current_user, get_db
from .rate_limit import get_login_rate_limiter

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
router = APIRouter()


def _render(request: Request, template: str, **context):
    context.setdefault("csrf_token", get_csrf_token(request))
    return templates.TemplateResponse(request=request, name=template, context=context)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", error=None)


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    limiter = get_login_rate_limiter()
    client_key = request.client.host if request.client else "unknown"
    if not limiter.allowed(client_key):
        return _render(request, "login.html", error="登录尝试过于频繁，请稍后再试")
    user = authenticate(db, email, password)
    if user is None:
        limiter.record_failure(client_key)
        return _render(request, "login.html", error="账号或密码错误")
    limiter.clear(client_key)
    request.session.clear()
    request.session["user_id"] = user.id
    db.add(
        AuditLog(actor_user_id=user.id, action="login", target_type="user", target_id=str(user.id))
    )
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request, csrf_token: str | None = Form(None)):
    validate_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    reports = list(
        db.scalars(
            select(Report)
            .where(Report.user_id == user.id)
            .order_by(Report.created_at.desc())
            .limit(5)
        )
    )
    mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
    message_count = db.scalar(
        select(func.count())
        .select_from(CanonicalMessage)
        .where(CanonicalMessage.owner_user_id == user.id)
    )
    return _render(
        request,
        "dashboard.html",
        user=user,
        reports=reports,
        mailbox=mailbox,
        message_count=message_count or 0,
        error=mailbox.sync_error if mailbox and mailbox.sync_error else None,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
    return _render(
        request,
        "settings.html",
        user=user,
        mailbox=mailbox,
        error=None,
        saved=False,
        tested=False,
    )


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    return _render_schedule_page(request, user, db, error=None)


def _render_schedule_page(request: Request, user: User, db: Session, error: str | None):
    mailboxes = list(db.scalars(select(Mailbox).where(Mailbox.user_id == user.id)))
    rule_sets = list(
        db.scalars(
            select(RuleSet)
            .where(RuleSet.user_id == user.id)
            .order_by(RuleSet.priority.asc(), RuleSet.id.asc())
        )
    )
    schedules = list(
        db.scalars(
            select(Schedule).where(Schedule.user_id == user.id).order_by(Schedule.created_at.desc())
        )
    )
    return _render(
        request,
        "schedules.html",
        user=user,
        mailboxes=mailboxes,
        rule_sets=rule_sets,
        schedules=schedules,
        error=error,
    )


@router.post("/schedules", response_class=HTMLResponse)
def create_schedule(
    request: Request,
    name: str = Form(...),
    mailbox_id: int = Form(...),
    rule_set_id: int | None = Form(None),
    schedule_type: str = Form("daily"),
    scheduled_time: str = Form("09:00"),
    weekdays: str = Form("mon-fri"),
    custom_cron: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
    lookback_hours: int = Form(24),
    is_enabled: bool = Form(False),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        if schedule_type not in {"daily", "weekly", "custom"}:
            raise ValueError("任务类型无效")
        if not 1 <= lookback_hours <= 24 * 31:
            raise ValueError("回看时间必须在 1 到 744 小时之间")
        mailbox = db.scalar(
            select(Mailbox).where(Mailbox.id == mailbox_id, Mailbox.user_id == user.id)
        )
        if mailbox is None:
            raise ValueError("邮箱不存在或不属于当前用户")
        selected_rule_id = None
        if rule_set_id is not None:
            rule_set = db.scalar(
                select(RuleSet).where(RuleSet.id == rule_set_id, RuleSet.user_id == user.id)
            )
            if rule_set is None:
                raise ValueError("规则集不存在或不属于当前用户")
            selected_rule_id = rule_set.id
        cron_expression = build_cron_expression(
            schedule_type, scheduled_time, weekdays, custom_cron
        )
        validate_schedule(cron_expression, timezone.strip())
        db.add(
            Schedule(
                user_id=user.id,
                mailbox_id=mailbox.id,
                rule_set_id=selected_rule_id,
                name=name.strip() or "邮件报告任务",
                cron_expression=cron_expression,
                timezone=timezone.strip(),
                lookback_hours=lookback_hours,
                is_enabled=is_enabled,
            )
        )
        db.commit()
        return RedirectResponse("/schedules", status_code=303)
    except ValueError as exc:
        db.rollback()
        return _render_schedule_page(request, user, db, error=f"任务配置无效：{exc}")


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(
    request: Request,
    schedule_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    schedule = db.scalar(
        select(Schedule).where(Schedule.id == schedule_id, Schedule.user_id == user.id)
    )
    if schedule is None:
        return HTMLResponse("任务不存在", status_code=404)
    schedule.is_enabled = not schedule.is_enabled
    db.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rule_sets = list(
        db.scalars(
            select(RuleSet).where(RuleSet.user_id == user.id).order_by(RuleSet.priority.asc())
        )
    )
    return _render(
        request,
        "rules.html",
        user=user,
        rule_sets=rule_sets,
        default_definition=json.dumps(MATCH_ALL, ensure_ascii=False, indent=2),
        error=None,
    )


@router.post("/rules", response_class=HTMLResponse)
def create_rule_set(
    request: Request,
    name: str = Form(...),
    definition: str = Form(...),
    priority: int = Form(100),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        parsed = json.loads(definition)
        RuleService(db).validate(parsed)
        db.add(RuleSet(user_id=user.id, name=name.strip(), definition=parsed, priority=priority))
        db.commit()
        return RedirectResponse("/rules", status_code=303)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        rule_sets = list(
            db.scalars(
                select(RuleSet).where(RuleSet.user_id == user.id).order_by(RuleSet.priority.asc())
            )
        )
        return _render(
            request,
            "rules.html",
            user=user,
            rule_sets=rule_sets,
            default_definition=definition,
            error=f"规则无效：{exc}",
        )


@router.post("/settings", response_class=HTMLResponse)
def save_settings(
    request: Request,
    email_address: str = Form(...),
    imap_host: str = Form(...),
    imap_port: int = Form(993),
    username: str = Form(...),
    password: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(465),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    validate_csrf(request, csrf_token)
    mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
    if mailbox is None:
        if not password:
            return _render(
                request,
                "settings.html",
                user=user,
                mailbox=None,
                error="首次配置必须填写邮箱密码",
                saved=False,
                tested=False,
            )
        mailbox = Mailbox(
            user_id=user.id,
            name="默认邮箱",
            email_address=email_address.strip(),
            imap_host=imap_host.strip(),
            imap_port=imap_port,
            username=username.strip(),
            smtp_host=smtp_host.strip(),
            smtp_port=smtp_port,
            credential_encrypted=encrypt_secret(password),
        )
        db.add(mailbox)
    else:
        mailbox.email_address = email_address.strip()
        mailbox.imap_host = imap_host.strip()
        mailbox.imap_port = imap_port
        mailbox.username = username.strip()
        mailbox.smtp_host = smtp_host.strip()
        mailbox.smtp_port = smtp_port
        if password:
            mailbox.credential_encrypted = encrypt_secret(password)
    db.commit()
    return _render(
        request,
        "settings.html",
        user=user,
        mailbox=mailbox,
        error=None,
        saved=True,
        tested=False,
    )


@router.post("/settings/test")
def test_settings_connection(
    request: Request,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
    if mailbox is None:
        return RedirectResponse("/settings", status_code=303)
    try:
        settings = get_settings()
        password = decrypt_secret(mailbox.credential_encrypted, settings)
        IMAPConnector(
            MailboxConnection(
                host=mailbox.imap_host,
                port=mailbox.imap_port,
                username=mailbox.username,
                password=password,
                tls=mailbox.imap_tls,
                folder=mailbox.folder,
            )
        ).test_connection()
        return _render(
            request,
            "settings.html",
            user=user,
            mailbox=mailbox,
            error=None,
            saved=False,
            tested=True,
        )
    except Exception as exc:
        return _render(
            request,
            "settings.html",
            user=user,
            mailbox=mailbox,
            error=f"连接测试失败：{type(exc).__name__}",
            saved=False,
            tested=False,
        )


@router.post("/sync")
def sync_now(
    request: Request,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
    if mailbox is None:
        return RedirectResponse("/settings", status_code=303)
    try:
        settings = get_settings()
        password = decrypt_secret(mailbox.credential_encrypted, settings)
        connector = IMAPConnector(
            MailboxConnection(
                host=mailbox.imap_host,
                port=mailbox.imap_port,
                username=mailbox.username,
                password=password,
                tls=mailbox.imap_tls,
                folder=mailbox.folder,
            )
        )
        MailSyncService(db, settings).sync(mailbox, connector)
        db.commit()
    except Exception as exc:
        db.rollback()
        mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
        if mailbox:
            mailbox.sync_error = f"{type(exc).__name__}: 邮箱同步失败"
            db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/demo/seed")
def demo_seed(
    request: Request,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    seed_demo(db, user, get_settings().data_dir)
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/reports/generate")
def generate_report(
    request: Request,
    use_demo_provider: bool = Form(False),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    service = ReportService(db)
    try:
        service.generate_for_user(user, use_demo_provider=use_demo_provider)
        db.commit()
    except (PermissionError, RuntimeError, ValueError) as exc:
        db.rollback()
        reports = list(
            db.scalars(
                select(Report)
                .where(Report.user_id == user.id)
                .order_by(Report.created_at.desc())
                .limit(100)
            )
        )
        return _render(request, "reports.html", user=user, reports=reports, error=str(exc))
    return RedirectResponse("/reports", status_code=303)


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    reports = list(
        db.scalars(
            select(Report)
            .where(Report.user_id == user.id)
            .order_by(Report.created_at.desc())
            .limit(100)
        )
    )
    return _render(request, "reports.html", user=user, reports=reports, error=None)


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail(
    request: Request,
    report_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    report = db.scalar(select(Report).where(Report.id == report_id, Report.user_id == user.id))
    if report is None:
        return HTMLResponse("报告不存在", status_code=404)
    deliveries = list(
        db.scalars(
            select(Delivery)
            .where(Delivery.report_id == report.id)
            .order_by(Delivery.created_at.desc())
        )
    )
    return _render(
        request,
        "report_detail.html",
        user=user,
        report=report,
        deliveries=deliveries,
        error=request.query_params.get("delivery_error"),
    )


@router.post("/reports/{report_id}/send")
def send_report(
    request: Request,
    report_id: int,
    recipient: str = Form(""),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    report = db.scalar(select(Report).where(Report.id == report_id, Report.user_id == user.id))
    if report is None:
        return HTMLResponse("报告不存在", status_code=404)
    mailbox = db.scalar(
        select(Mailbox).where(Mailbox.id == report.mailbox_id, Mailbox.user_id == user.id)
    )
    if mailbox is None or not mailbox.smtp_host:
        return RedirectResponse(
            f"/reports/{report_id}?delivery_error={quote('SMTP 尚未配置')}", status_code=303
        )
    try:
        delivery = ReportDeliveryService(db).send_report(
            report, mailbox, recipient.strip() or user.email
        )
        db.commit()
        return RedirectResponse(f"/reports/{report_id}?delivery={delivery.status}", status_code=303)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            f"/reports/{report_id}?delivery_error={quote(str(exc))}", status_code=303
        )


@router.post("/deliveries/{delivery_id}/retry")
def retry_report_delivery(
    request: Request,
    delivery_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    delivery = db.scalar(
        select(Delivery)
        .join(Report, Report.id == Delivery.report_id)
        .where(Delivery.id == delivery_id, Report.user_id == user.id)
    )
    if delivery is None:
        return HTMLResponse("投递记录不存在", status_code=404)
    report = db.get(Report, delivery.report_id)
    mailbox = db.scalar(
        select(Mailbox).where(Mailbox.id == report.mailbox_id, Mailbox.user_id == user.id)
    )
    if mailbox is None or not mailbox.smtp_host:
        return RedirectResponse(
            f"/reports/{report.id}?delivery_error={quote('SMTP 尚未配置')}", status_code=303
        )
    ReportDeliveryService(db).retry_delivery(delivery, report, mailbox)
    db.commit()
    return RedirectResponse(f"/reports/{report.id}?delivery={delivery.status}", status_code=303)


@router.get("/messages", response_class=HTMLResponse)
def messages_page(
    request: Request,
    q: str = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    messages = SearchService(db).search(user.id, q)
    return _render(request, "messages.html", user=user, messages=messages, query=q)


def _owned_message(db: Session, user: User, message_id: int) -> CanonicalMessage | None:
    return db.scalar(
        select(CanonicalMessage).where(
            CanonicalMessage.id == message_id,
            CanonicalMessage.owner_user_id == user.id,
        )
    )


def _safe_referer(request: Request, fallback: str = "/messages") -> str:
    value = request.headers.get("referer", "")
    return value if value.startswith("/") and not value.startswith("//") else fallback


@router.post("/messages/{message_id}/toggle-processed")
def toggle_message_processed(
    request: Request,
    message_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    message = _owned_message(db, user, message_id)
    if message is None:
        return HTMLResponse("邮件不存在", status_code=404)
    message.local_processed = not message.local_processed
    db.commit()
    return RedirectResponse(_safe_referer(request), status_code=303)


@router.post("/messages/{message_id}/toggle-starred")
def toggle_message_starred(
    request: Request,
    message_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    message = _owned_message(db, user, message_id)
    if message is None:
        return HTMLResponse("邮件不存在", status_code=404)
    message.local_starred = not message.local_starred
    db.commit()
    return RedirectResponse(_safe_referer(request), status_code=303)


@router.post("/messages/{message_id}/labels")
def add_message_label(
    request: Request,
    message_id: int,
    label: str = Form(...),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    message = _owned_message(db, user, message_id)
    if message is None:
        return HTMLResponse("邮件不存在", status_code=404)
    normalized = label.strip()[:80]
    if normalized and normalized not in message.local_labels:
        message.local_labels = [*message.local_labels, normalized]
        db.commit()
    return RedirectResponse(_safe_referer(request), status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return _render_admin_page(request, user, db, error=None)


def _render_admin_page(request: Request, user: User, db: Session, error: str | None):
    users = list(db.scalars(select(User).order_by(User.created_at.desc())))
    jobs = list(db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(20)))
    profiles = list(
        db.scalars(select(AIProviderProfile).order_by(AIProviderProfile.created_at.desc()))
    )
    return _render(
        request,
        "admin.html",
        user=user,
        users=users,
        jobs=jobs,
        profiles=profiles,
        error=error,
    )


@router.post("/admin/users", response_class=HTMLResponse)
def create_managed_user(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    role: str = Form("user"),
    csrf_token: str | None = Form(None),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        if role not in {"user", "admin"}:
            raise ValueError("账号角色必须是 user 或 admin")
        created = create_user(db, email, password, display_name, role=role)
        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="user_create",
                target_type="user",
                target_id=str(created.id),
                metadata_json={"role": created.role},
            )
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _render_admin_page(request, user, db, error=f"账号创建失败：{exc}")
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/models")
def create_model_profile(
    request: Request,
    name: str = Form(...),
    role: str = Form("primary"),
    base_url: str = Form(...),
    model_name: str = Form(""),
    api_key: str = Form(""),
    image_input: bool = Form(False),
    structured_output: bool = Form(True),
    timeout_seconds: float = Form(90.0),
    max_retries: int = Form(2),
    max_input_chars: int = Form(120_000),
    max_output_tokens: int = Form(1_800),
    max_images: int = Form(20),
    max_image_size_mb: int = Form(10),
    csrf_token: str | None = Form(None),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    validation_error = None
    if role not in {"primary", "vision"}:
        validation_error = "模型角色必须是 primary 或 vision"
    elif not 1 <= timeout_seconds <= 600:
        validation_error = "模型超时时间必须在 1 到 600 秒之间"
    elif not 0 <= max_retries <= 5:
        validation_error = "模型重试次数必须在 0 到 5 次之间"
    elif not 4_096 <= max_input_chars <= 2_000_000:
        validation_error = "模型输入上限必须在 4096 到 2000000 字符之间"
    elif not 128 <= max_output_tokens <= 32_000:
        validation_error = "模型输出上限必须在 128 到 32000 token 之间"
    elif not 1 <= max_images <= 100:
        validation_error = "模型图片数量上限必须在 1 到 100 张之间"
    elif not 1 <= max_image_size_mb <= 100:
        validation_error = "单图片大小上限必须在 1 到 100 MB 之间"
    if validation_error:
        return _render_admin_page(request, user, db, error=validation_error)
    profile = AIProviderProfile(
        owner_user_id=None,
        name=name.strip(),
        base_url=base_url.strip().rstrip("/"),
        model_name=model_name.strip(),
        api_key_encrypted=encrypt_secret(api_key) if api_key else None,
        capabilities={
            "text_input": True,
            "image_input": image_input,
            "structured_output": structured_output,
            "strict_json_schema": False,
        },
        policy={
            "timeout_seconds": timeout_seconds,
            "max_retries": max_retries,
            "max_input_chars": max_input_chars,
            "max_output_tokens": max_output_tokens,
            "max_images": max_images,
            "max_image_bytes": max_image_size_mb * 1024 * 1024,
        },
    )
    db.add(profile)
    db.flush()
    db.add(ModelBinding(role=role, provider_profile_id=profile.id))
    db.commit()
    return RedirectResponse("/admin", status_code=303)
