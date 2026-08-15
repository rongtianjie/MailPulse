from __future__ import annotations

import json
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import authenticate, create_user, set_password
from ..config import get_settings
from ..delivery import ReportDeliveryService, SMTPConfig, SMTPDeliveryProvider
from ..demo import seed_demo
from ..errors import error_message
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
    ScheduleDeliveryTarget,
    User,
)
from ..report_service import ReportService
from ..rules import MATCH_ALL, RuleService
from ..search import SearchService
from ..security import decrypt_secret, encrypt_secret
from ..worker import build_cron_expression, validate_schedule
from .csrf import get_csrf_token, validate_csrf
from .deps import admin_user, current_user, get_db
from .rate_limit import get_login_rate_limiter, get_register_rate_limiter

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
router = APIRouter()

DEFAULT_TIMEZONE = "Asia/Shanghai"
MESSAGES_PAGE_SIZE = 50
REMEMBER_PASSWORD_COOKIE = "mailpulse_remember_credentials"


def _remember_password_saved(request: Request, settings) -> tuple[str, str] | None:
    """Return (username, password) saved by the remember-password cookie, or None."""
    payload = request.cookies.get(REMEMBER_PASSWORD_COOKIE)
    if not payload:
        return None
    try:
        value = decrypt_secret(payload, settings)
    except ValueError:
        return None
    username, separator, password = value.partition("\n")
    if not separator or not username or not password:
        return None
    return username, password


def _set_remember_password_cookie(response, username: str, password: str, settings) -> None:
    response.set_cookie(
        REMEMBER_PASSWORD_COOKIE,
        encrypt_secret(f"{username}\n{password}", settings),
        max_age=settings.remember_password_days * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=settings.session_https_only,
        path="/",
    )


def _clear_remember_password_cookie(response) -> None:
    response.delete_cookie(REMEMBER_PASSWORD_COOKIE, path="/")


def _fmt_time(value, tz: str = DEFAULT_TIMEZONE) -> str:
    """Render a stored datetime in the user-facing timezone."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")


templates.env.filters["fmt_time"] = _fmt_time


def _render(request: Request, template: str, **context):
    context.setdefault("csrf_token", get_csrf_token(request))
    return templates.TemplateResponse(request=request, name=template, context=context)


def _build_imap_connector(mailbox: Mailbox, settings) -> IMAPConnector:
    password = decrypt_secret(mailbox.credential_encrypted, settings)
    return IMAPConnector(
        MailboxConnection(
            host=mailbox.imap_host,
            port=mailbox.imap_port,
            username=mailbox.username,
            password=password,
            tls=mailbox.imap_tls,
            folder=mailbox.folder,
        )
    )


def _login_redirect(user: User) -> str:
    if user.role == "admin":
        return "/admin/account/password" if user.must_change_password else "/admin"
    return "/"


def _login_page_context(
    request: Request,
    *,
    error: str | None,
    notice: str | None = None,
    username: str | None = None,
    remember_me: bool = False,
    remember_password: bool = False,
) -> dict:
    """Build the login template context, prefilling saved credentials when present."""
    settings = get_settings()
    saved = _remember_password_saved(request, settings)
    saved_username = saved[0] if saved else None
    saved_password = (
        saved[1]
        if saved and (username is None or saved[0] == username.strip().lower())
        else None
    )
    return {
        "error": error,
        "notice": notice,
        "username": username,
        "saved_username": saved_username,
        "saved_password": saved_password,
        "remember_me": remember_me,
        "remember_password": remember_password or bool(saved_password),
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if user_id:
        user = db.get(User, int(user_id))
        if user and user.is_active:
            return RedirectResponse(_login_redirect(user), status_code=303)
        request.session.clear()
    return _render(
        request,
        "login.html",
        **_login_page_context(request, error=None, notice=request.query_params.get("registered")),
    )


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember_me: bool = Form(False),
    remember_password: bool = Form(False),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    limiter = get_login_rate_limiter()
    client_key = request.client.host if request.client else "unknown"
    if not limiter.allowed(client_key):
        return _render(
            request,
            "login.html",
            **_login_page_context(
                request, error="登录尝试过于频繁，请稍后再试", username=username,
                remember_me=remember_me, remember_password=remember_password,
            ),
        )
    user = authenticate(db, username, password)
    if user is None:
        limiter.record_failure(client_key)
        return _render(
            request,
            "login.html",
            **_login_page_context(
                request, error="账号或密码错误", username=username,
                remember_me=remember_me, remember_password=remember_password,
            ),
        )
    limiter.clear(client_key)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["remember_me"] = remember_me
    db.add(
        AuditLog(actor_user_id=user.id, action="login", target_type="user", target_id=str(user.id))
    )
    db.commit()
    response = RedirectResponse(_login_redirect(user), status_code=303)
    if remember_password:
        _set_remember_password_cookie(response, user.username, password, get_settings())
    else:
        _clear_remember_password_cookie(response)
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str | None = Form(None)):
    validate_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if user_id:
        user = db.get(User, int(user_id))
        if user and user.is_active:
            return RedirectResponse(_login_redirect(user), status_code=303)
        request.session.clear()
    return _render(request, "register.html", error=None)


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(""),
    display_name: str = Form(""),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    limiter = get_register_rate_limiter()
    client_key = request.client.host if request.client else "unknown"
    if not limiter.allowed(client_key):
        return _render(request, "register.html", error="注册尝试过于频繁，请稍后再试")
    if password != confirm_password:
        return _render(
            request,
            "register.html",
            error="两次输入的密码不一致",
            username=username,
            display_name=display_name,
        )
    try:
        # 自助注册只允许创建普通用户，不允许注册管理员账号
        created = create_user(db, username, password, display_name, role="user")
        db.add(
            AuditLog(
                actor_user_id=created.id,
                action="user_register",
                target_type="user",
                target_id=str(created.id),
                metadata_json={"role": created.role},
            )
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        limiter.record_failure(client_key)
        return _render(
            request,
            "register.html",
            error=f"注册失败：{exc}",
            username=username,
            display_name=display_name,
        )
    limiter.clear(client_key)
    return RedirectResponse("/login?registered=1", status_code=303)


@router.get("/admin/account/password", response_class=HTMLResponse)
def admin_password_page(request: Request, user: User = Depends(admin_user)):
    return _render(request, "admin_password.html", user=user, error=None)


@router.post("/admin/account/password", response_class=HTMLResponse)
def change_admin_password(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str | None = Form(None),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    if new_password != confirm_password:
        return _render(request, "admin_password.html", user=user, error="两次输入的密码不一致")
    try:
        set_password(user, new_password)
        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="admin_password_change",
                target_type="user",
                target_id=str(user.id),
            )
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _render(request, "admin_password.html", user=user, error=str(exc))
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/account/password/skip")
def skip_admin_password_change(
    request: Request,
    csrf_token: str | None = Form(None),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user.must_change_password = False
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="admin_password_change_skip",
            target_type="user",
            target_id=str(user.id),
        )
    )
    db.commit()
    return RedirectResponse("/admin", status_code=303)


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
        tested_smtp=False,
    )


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    return _render_schedule_page(request, user, db, error=None)


def _render_schedule_page(
    request: Request,
    user: User,
    db: Session,
    error: str | None,
    editing_schedule: Schedule | None = None,
    submitted: dict | None = None,
):
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
    if submitted is not None:
        form = submitted
    elif editing_schedule is not None:
        form = _schedule_to_form(editing_schedule)
    else:
        form = {
            "name": "每日邮件报告",
            "mailbox_id": "",
            "rule_set_id": "",
            "schedule_type": "daily",
            "scheduled_time": "09:00",
            "weekdays": "mon-fri",
            "custom_cron": "",
            "timezone": "Asia/Shanghai",
            "lookback_hours": 24,
            "is_enabled": True,
        }
    delivery_targets = (
        list(editing_schedule.delivery_targets) if editing_schedule is not None else []
    )
    return _render(
        request,
        "schedules.html",
        user=user,
        mailboxes=mailboxes,
        rule_sets=rule_sets,
        schedules=schedules,
        error=error,
        form=form,
        editing_schedule=editing_schedule,
        delivery_targets=delivery_targets,
    )


WEEKDAY_PRESETS = {"mon-fri", "mon-sun", "sat,sun", "mon,wed,fri"}


def _schedule_to_form(schedule: Schedule) -> dict:
    """Derive editable form fields from a stored schedule (lossy for custom cron)."""
    form = {
        "name": schedule.name,
        "mailbox_id": schedule.mailbox_id,
        "rule_set_id": schedule.rule_set_id or "",
        "timezone": schedule.timezone,
        "lookback_hours": schedule.lookback_hours,
        "is_enabled": schedule.is_enabled,
        "schedule_type": "custom",
        "scheduled_time": "09:00",
        "weekdays": "mon-fri",
        "custom_cron": schedule.cron_expression,
    }
    parts = schedule.cron_expression.split()
    if len(parts) == 5 and parts[2] == "*" and parts[3] == "*":
        minute, hour, _, _, day_of_week = parts
        try:
            hour_int, minute_int = int(hour), int(minute)
            if 0 <= hour_int <= 23 and 0 <= minute_int <= 59:
                form["scheduled_time"] = f"{hour_int:02d}:{minute_int:02d}"
                if day_of_week == "*":
                    form["schedule_type"] = "daily"
                    form["custom_cron"] = ""
                elif day_of_week in WEEKDAY_PRESETS:
                    form["schedule_type"] = "weekly"
                    form["weekdays"] = day_of_week
                    form["custom_cron"] = ""
        except ValueError:
            pass
    return form


def _submitted_schedule_form(
    name: str,
    mailbox_id: int,
    rule_set_id: int | None,
    schedule_type: str,
    scheduled_time: str,
    weekdays: str,
    custom_cron: str,
    timezone: str,
    lookback_hours: int,
    is_enabled: bool,
) -> dict:
    return {
        "name": name,
        "mailbox_id": mailbox_id,
        "rule_set_id": rule_set_id or "",
        "schedule_type": schedule_type,
        "scheduled_time": scheduled_time,
        "weekdays": weekdays,
        "custom_cron": custom_cron,
        "timezone": timezone,
        "lookback_hours": lookback_hours,
        "is_enabled": is_enabled,
    }


def _collect_schedule_form(
    name: str,
    mailbox_id: int,
    rule_set_id: int | None,
    schedule_type: str,
    scheduled_time: str,
    weekdays: str,
    custom_cron: str,
    timezone: str,
    lookback_hours: int,
    is_enabled: bool,
    user: User,
    db: Session,
) -> dict:
    """Validate schedule form inputs; raises ValueError with a user-facing reason."""
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
    cron_expression = build_cron_expression(schedule_type, scheduled_time, weekdays, custom_cron)
    validate_schedule(cron_expression, timezone.strip())
    return {
        "name": name.strip() or "邮件报告任务",
        "mailbox_id": mailbox.id,
        "rule_set_id": selected_rule_id,
        "cron_expression": cron_expression,
        "timezone": timezone.strip(),
        "lookback_hours": lookback_hours,
        "is_enabled": is_enabled,
    }


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
        values = _collect_schedule_form(
            name, mailbox_id, rule_set_id, schedule_type, scheduled_time,
            weekdays, custom_cron, timezone, lookback_hours, is_enabled, user, db,
        )
        schedule = Schedule(user_id=user.id, **values)
        db.add(schedule)
        db.commit()
        return RedirectResponse(f"/schedules/{schedule.id}/edit", status_code=303)
    except ValueError as exc:
        db.rollback()
        submitted = _submitted_schedule_form(
            name, mailbox_id, rule_set_id, schedule_type, scheduled_time,
            weekdays, custom_cron, timezone, lookback_hours, is_enabled,
        )
        return _render_schedule_page(
            request, user, db, error=f"任务配置无效：{exc}", submitted=submitted
        )


@router.get("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
def edit_schedule_page(
    request: Request,
    schedule_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    schedule = db.scalar(
        select(Schedule).where(Schedule.id == schedule_id, Schedule.user_id == user.id)
    )
    if schedule is None:
        return HTMLResponse("任务不存在", status_code=404)
    return _render_schedule_page(request, user, db, error=None, editing_schedule=schedule)


@router.post("/schedules/{schedule_id}/edit")
def update_schedule(
    request: Request,
    schedule_id: int,
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
    schedule = db.scalar(
        select(Schedule).where(Schedule.id == schedule_id, Schedule.user_id == user.id)
    )
    if schedule is None:
        return HTMLResponse("任务不存在", status_code=404)
    try:
        values = _collect_schedule_form(
            name, mailbox_id, rule_set_id, schedule_type, scheduled_time,
            weekdays, custom_cron, timezone, lookback_hours, is_enabled, user, db,
        )
        schedule.name = values["name"]
        schedule.mailbox_id = values["mailbox_id"]
        schedule.rule_set_id = values["rule_set_id"]
        schedule.cron_expression = values["cron_expression"]
        schedule.timezone = values["timezone"]
        schedule.lookback_hours = values["lookback_hours"]
        schedule.is_enabled = values["is_enabled"]
        db.commit()
        return RedirectResponse(f"/schedules/{schedule.id}/edit", status_code=303)
    except ValueError as exc:
        db.rollback()
        submitted = _submitted_schedule_form(
            name, mailbox_id, rule_set_id, schedule_type, scheduled_time,
            weekdays, custom_cron, timezone, lookback_hours, is_enabled,
        )
        return _render_schedule_page(
            request, user, db, error=f"任务配置无效：{exc}",
            editing_schedule=schedule, submitted=submitted,
        )


@router.post("/schedules/{schedule_id}/targets")
def add_delivery_target(
    request: Request,
    schedule_id: int,
    destination: str = Form(...),
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
    destination = destination.strip().lower()
    if "@" not in destination:
        return _render_schedule_page(
            request, user, db, error="投递邮箱格式无效", editing_schedule=schedule
        )
    if any(item.destination == destination for item in schedule.delivery_targets):
        return _render_schedule_page(
            request, user, db, error="该投递邮箱已存在", editing_schedule=schedule
        )
    db.add(
        ScheduleDeliveryTarget(
            schedule_id=schedule.id, channel="smtp", destination=destination
        )
    )
    db.commit()
    return RedirectResponse(f"/schedules/{schedule.id}/edit", status_code=303)


@router.post("/schedules/{schedule_id}/targets/{target_id}/toggle")
def toggle_delivery_target(
    request: Request,
    schedule_id: int,
    target_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    target = db.scalar(
        select(ScheduleDeliveryTarget)
        .join(Schedule, Schedule.id == ScheduleDeliveryTarget.schedule_id)
        .where(
            ScheduleDeliveryTarget.id == target_id,
            Schedule.id == schedule_id,
            Schedule.user_id == user.id,
        )
    )
    if target is None:
        return HTMLResponse("投递目标不存在", status_code=404)
    target.is_enabled = not target.is_enabled
    db.commit()
    return RedirectResponse(f"/schedules/{schedule_id}/edit", status_code=303)


@router.post("/schedules/{schedule_id}/targets/{target_id}/delete")
def delete_delivery_target(
    request: Request,
    schedule_id: int,
    target_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    target = db.scalar(
        select(ScheduleDeliveryTarget)
        .join(Schedule, Schedule.id == ScheduleDeliveryTarget.schedule_id)
        .where(
            ScheduleDeliveryTarget.id == target_id,
            Schedule.id == schedule_id,
            Schedule.user_id == user.id,
        )
    )
    if target is None:
        return HTMLResponse("投递目标不存在", status_code=404)
    db.delete(target)
    db.commit()
    return RedirectResponse(f"/schedules/{schedule_id}/edit", status_code=303)


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


@router.post("/schedules/{schedule_id}/delete")
def delete_schedule(
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
    db.delete(schedule)
    db.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _render_rules_page(
        request,
        user,
        db,
        definition=json.dumps(MATCH_ALL, ensure_ascii=False, indent=2),
        error=None,
    )


def _render_rules_page(
    request: Request,
    user: User,
    db: Session,
    definition: str,
    error: str | None,
    name: str | None = None,
    priority: int | None = None,
    editing_rule: RuleSet | None = None,
):
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
        definition=definition,
        error=error,
        form_name=name,
        form_priority=priority,
        editing_rule=editing_rule,
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
        return _render_rules_page(
            request,
            user,
            db,
            definition=definition,
            error=f"规则无效：{exc}",
            name=name,
            priority=priority,
        )


@router.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
def edit_rule_page(
    request: Request,
    rule_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    rule_set = db.scalar(
        select(RuleSet).where(RuleSet.id == rule_id, RuleSet.user_id == user.id)
    )
    if rule_set is None:
        return HTMLResponse("规则集不存在", status_code=404)
    return _render_rules_page(
        request,
        user,
        db,
        definition=json.dumps(rule_set.definition, ensure_ascii=False, indent=2),
        error=None,
        name=rule_set.name,
        priority=rule_set.priority,
        editing_rule=rule_set,
    )


@router.post("/rules/{rule_id}/edit")
def update_rule_set(
    request: Request,
    rule_id: int,
    name: str = Form(...),
    definition: str = Form(...),
    priority: int = Form(100),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    rule_set = db.scalar(
        select(RuleSet).where(RuleSet.id == rule_id, RuleSet.user_id == user.id)
    )
    if rule_set is None:
        return HTMLResponse("规则集不存在", status_code=404)
    try:
        parsed = json.loads(definition)
        RuleService(db).validate(parsed)
        rule_set.name = name.strip()
        rule_set.priority = priority
        rule_set.definition = parsed
        db.commit()
        return RedirectResponse("/rules", status_code=303)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _render_rules_page(
            request,
            user,
            db,
            definition=definition,
            error=f"规则无效：{exc}",
            name=name,
            priority=priority,
            editing_rule=rule_set,
        )


@router.post("/rules/{rule_id}/delete")
def delete_rule_set(
    request: Request,
    rule_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    rule_set = db.scalar(
        select(RuleSet).where(RuleSet.id == rule_id, RuleSet.user_id == user.id)
    )
    if rule_set is None:
        return HTMLResponse("规则集不存在", status_code=404)
    db.delete(rule_set)
    db.commit()
    return RedirectResponse("/rules", status_code=303)


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
                tested_smtp=False,
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
        tested_smtp=False,
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
        _build_imap_connector(mailbox, get_settings()).test_connection()
        return _render(
            request,
            "settings.html",
            user=user,
            mailbox=mailbox,
            error=None,
            saved=False,
            tested=True,
            tested_smtp=False,
        )
    except Exception as exc:
        return _render(
            request,
            "settings.html",
            user=user,
            mailbox=mailbox,
            error=f"连接测试失败：{error_message(exc)}",
            saved=False,
            tested=False,
            tested_smtp=False,
        )


@router.post("/settings/test-smtp")
def test_smtp_connection(
    request: Request,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
    if mailbox is None:
        return RedirectResponse("/settings", status_code=303)
    if not mailbox.smtp_host:
        return _render(
            request,
            "settings.html",
            user=user,
            mailbox=mailbox,
            error="SMTP 主机未配置，无法验证连接",
            saved=False,
            tested=False,
            tested_smtp=False,
        )
    try:
        password = decrypt_secret(mailbox.credential_encrypted, get_settings())
        SMTPDeliveryProvider(
            SMTPConfig(
                host=mailbox.smtp_host,
                port=mailbox.smtp_port,
                username=mailbox.username,
                password=password,
                use_tls=mailbox.smtp_tls,
            )
        ).test_connection()
        return _render(
            request,
            "settings.html",
            user=user,
            mailbox=mailbox,
            error=None,
            saved=False,
            tested=False,
            tested_smtp=True,
        )
    except Exception as exc:
        return _render(
            request,
            "settings.html",
            user=user,
            mailbox=mailbox,
            error=f"SMTP 连接测试失败：{error_message(exc)}",
            saved=False,
            tested=False,
            tested_smtp=False,
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
        connector = _build_imap_connector(mailbox, get_settings())
        MailSyncService(db, get_settings()).sync(mailbox, connector)
        db.commit()
    except Exception as exc:
        db.rollback()
        mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
        if mailbox:
            mailbox.sync_error = error_message(exc, "邮箱同步失败")
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
    targets = []
    if report.schedule_id:
        schedule = db.get(Schedule, report.schedule_id)
        if schedule is not None:
            targets = [item for item in schedule.delivery_targets if item.is_enabled]
    return _render(
        request,
        "report_detail.html",
        user=user,
        report=report,
        deliveries=deliveries,
        delivery_targets=targets,
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
    recipient = recipient.strip()
    if not recipient:
        return RedirectResponse(
            f"/reports/{report_id}?delivery_error={quote('请填写报告收件人')}", status_code=303
        )
    try:
        delivery = ReportDeliveryService(db).send_report(report, mailbox, recipient)
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
    status: str = "",
    page: int = 1,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    status = status if status in {"unprocessed", "processed", "starred"} else ""
    page = max(1, page)
    service = SearchService(db)
    total = service.count(user.id, q, status)
    pages = max(1, ceil(total / MESSAGES_PAGE_SIZE))
    page = min(page, pages)
    messages = service.search(
        user.id,
        q,
        status=status,
        limit=MESSAGES_PAGE_SIZE,
        offset=(page - 1) * MESSAGES_PAGE_SIZE,
    )
    return _render(
        request,
        "messages.html",
        user=user,
        messages=messages,
        query=q,
        status=status,
        page=page,
        pages=pages,
        total=total,
    )


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


@router.post("/messages/{message_id}/labels/delete")
def delete_message_label(
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
    if label in message.local_labels:
        message.local_labels = [item for item in message.local_labels if item != label]
        db.commit()
    return RedirectResponse(_safe_referer(request), status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    users = list(db.scalars(select(User).order_by(User.created_at.desc())))
    profiles = list(
        db.scalars(select(AIProviderProfile).order_by(AIProviderProfile.created_at.desc()))
    )
    jobs = list(db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(5)))
    return _render(
        request,
        "admin.html",
        user=user,
        users_count=len(users),
        active_users_count=sum(item.is_active for item in users),
        profiles_count=len(profiles),
        enabled_profiles_count=sum(item.is_enabled for item in profiles),
        jobs=jobs,
    )


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(
    request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)
):
    return _render_admin_users_page(request, user, db, error=None)


def _render_admin_users_page(
    request: Request,
    user: User,
    db: Session,
    error: str | None,
):
    users = list(db.scalars(select(User).order_by(User.created_at.desc())))
    return _render(request, "admin_users.html", user=user, users=users, error=error)


@router.get("/admin/models", response_class=HTMLResponse)
def admin_models_page(
    request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)
):
    return _render_admin_models_page(request, user, db, error=None)


def _render_admin_models_page(
    request: Request,
    user: User,
    db: Session,
    error: str | None,
    editing_profile: AIProviderProfile | None = None,
    model_form: dict | None = None,
):
    profiles = list(
        db.scalars(select(AIProviderProfile).order_by(AIProviderProfile.created_at.desc()))
    )
    if model_form is None:
        model_form = _model_form_defaults(editing_profile, db)
    return _render(
        request,
        "admin_models.html",
        user=user,
        profiles=profiles,
        error=error,
        editing_profile=editing_profile,
        model_form=model_form,
    )


@router.get("/admin/jobs", response_class=HTMLResponse)
def admin_jobs_page(
    request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)
):
    jobs = list(db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(100)))
    return _render(request, "admin_jobs.html", user=user, jobs=jobs)


def _validate_model_form(
    role: str,
    timeout_seconds: float,
    max_retries: int,
    max_input_chars: int,
    max_output_tokens: int,
    max_images: int,
    max_image_size_mb: int,
) -> str | None:
    if role not in {"primary", "vision"}:
        return "模型角色必须是 primary 或 vision"
    if not 1 <= timeout_seconds <= 600:
        return "模型超时时间必须在 1 到 600 秒之间"
    if not 0 <= max_retries <= 5:
        return "模型重试次数必须在 0 到 5 次之间"
    if not 4_096 <= max_input_chars <= 2_000_000:
        return "模型输入上限必须在 4096 到 2000000 字符之间"
    if not 128 <= max_output_tokens <= 32_000:
        return "模型输出上限必须在 128 到 32000 token 之间"
    if not 1 <= max_images <= 100:
        return "模型图片数量上限必须在 1 到 100 张之间"
    if not 1 <= max_image_size_mb <= 100:
        return "单图片大小上限必须在 1 到 100 MB 之间"
    return None


def _model_form_values(
    name: str,
    role: str,
    base_url: str,
    model_name: str,
    image_input: bool,
    structured_output: bool,
    timeout_seconds: float,
    max_retries: int,
    max_input_chars: int,
    max_output_tokens: int,
    max_images: int,
    max_image_size_mb: int,
) -> dict:
    return {
        "name": name,
        "role": role,
        "base_url": base_url,
        "model_name": model_name,
        "api_key": "",
        "image_input": image_input,
        "structured_output": structured_output,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "max_input_chars": max_input_chars,
        "max_output_tokens": max_output_tokens,
        "max_images": max_images,
        "max_image_size_mb": max_image_size_mb,
    }


def _model_form_defaults(profile: AIProviderProfile | None, db: Session | None = None) -> dict:
    if profile is None:
        return _model_form_values(
            "", "primary", "", "", False, True, 90.0, 2, 120_000, 1800, 20, 10
        )
    capabilities = profile.capabilities or {}
    policy = profile.policy or {}
    role = "primary"
    if db is not None:
        binding = db.scalars(
            select(ModelBinding)
            .where(ModelBinding.provider_profile_id == profile.id)
            .limit(1)
        ).first()
        if binding is not None:
            role = binding.role
    max_image_bytes = policy.get("max_image_bytes", 10 * 1024 * 1024)
    return _model_form_values(
        profile.name,
        role,
        profile.base_url,
        profile.model_name or "",
        bool(capabilities.get("image_input", False)),
        bool(capabilities.get("structured_output", True)),
        policy.get("timeout_seconds", 90.0),
        policy.get("max_retries", 2),
        policy.get("max_input_chars", 120_000),
        policy.get("max_output_tokens", 1800),
        policy.get("max_images", 20),
        max_image_bytes // (1024 * 1024),
    )


@router.post("/admin/users", response_class=HTMLResponse)
def create_managed_user(
    request: Request,
    username: str = Form(...),
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
        created = create_user(db, username, password, display_name, role=role)
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
        return _render_admin_users_page(request, user, db, error=f"账号创建失败：{exc}")
    return RedirectResponse("/admin/users", status_code=303)


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
    validation_error = _validate_model_form(
        role, timeout_seconds, max_retries, max_input_chars, max_output_tokens,
        max_images, max_image_size_mb,
    )
    if validation_error:
        model_form = _model_form_values(
            name, role, base_url, model_name, image_input, structured_output,
            timeout_seconds, max_retries, max_input_chars, max_output_tokens,
            max_images, max_image_size_mb,
        )
        return _render_admin_models_page(
            request, user, db, error=validation_error, model_form=model_form
        )
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
    return RedirectResponse("/admin/models", status_code=303)


@router.get("/admin/models/{profile_id}/edit", response_class=HTMLResponse)
def edit_model_profile_page(
    request: Request,
    profile_id: int,
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    profile = db.get(AIProviderProfile, profile_id)
    if profile is None:
        return HTMLResponse("模型配置不存在", status_code=404)
    return _render_admin_models_page(request, user, db, error=None, editing_profile=profile)


@router.post("/admin/models/{profile_id}/edit")
def update_model_profile(
    request: Request,
    profile_id: int,
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
    profile = db.get(AIProviderProfile, profile_id)
    if profile is None:
        return HTMLResponse("模型配置不存在", status_code=404)
    validation_error = _validate_model_form(
        role, timeout_seconds, max_retries, max_input_chars, max_output_tokens,
        max_images, max_image_size_mb,
    )
    if validation_error:
        model_form = _model_form_values(
            name, role, base_url, model_name, image_input, structured_output,
            timeout_seconds, max_retries, max_input_chars, max_output_tokens,
            max_images, max_image_size_mb,
        )
        return _render_admin_models_page(
            request, user, db, error=validation_error,
            editing_profile=profile, model_form=model_form,
        )
    profile.name = name.strip()
    profile.base_url = base_url.strip().rstrip("/")
    profile.model_name = model_name.strip()
    if api_key:
        profile.api_key_encrypted = encrypt_secret(api_key)
    profile.capabilities = {
        "text_input": True,
        "image_input": image_input,
        "structured_output": structured_output,
        "strict_json_schema": False,
    }
    profile.policy = {
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "max_input_chars": max_input_chars,
        "max_output_tokens": max_output_tokens,
        "max_images": max_images,
        "max_image_bytes": max_image_size_mb * 1024 * 1024,
    }
    binding = db.scalars(
        select(ModelBinding)
        .where(ModelBinding.provider_profile_id == profile.id)
        .limit(1)
    ).first()
    if binding is None:
        db.add(ModelBinding(role=role, provider_profile_id=profile.id))
    else:
        binding.role = role
    db.commit()
    return RedirectResponse("/admin/models", status_code=303)


@router.post("/admin/models/{profile_id}/toggle")
def toggle_model_profile(
    request: Request,
    profile_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    profile = db.get(AIProviderProfile, profile_id)
    if profile is None:
        return HTMLResponse("模型配置不存在", status_code=404)
    profile.is_enabled = not profile.is_enabled
    db.commit()
    return RedirectResponse("/admin/models", status_code=303)


@router.post("/admin/models/{profile_id}/delete")
def delete_model_profile(
    request: Request,
    profile_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    profile = db.get(AIProviderProfile, profile_id)
    if profile is None:
        return HTMLResponse("模型配置不存在", status_code=404)
    db.delete(profile)
    db.commit()
    return RedirectResponse("/admin/models", status_code=303)


@router.post("/admin/users/{user_id}/toggle")
def toggle_user_active(
    request: Request,
    user_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    target = db.get(User, user_id)
    if target is None:
        return HTMLResponse("账号不存在", status_code=404)
    if target.id == user.id:
        return _render_admin_users_page(request, user, db, error="不能停用自己的账号")
    target.is_active = not target.is_active
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)
