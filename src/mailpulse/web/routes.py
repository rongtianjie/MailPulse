from __future__ import annotations

import json
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
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
    Task,
    TaskDeliveryTarget,
    User,
)
from ..rules import MATCH_ALL, RuleService
from ..search import SearchService
from ..security import decrypt_secret, encrypt_secret
from ..worker import build_cron_expression, next_fire_time, run_task_now, validate_schedule
from .csrf import get_csrf_token, validate_csrf
from .deps import admin_user, authenticated_user, current_user, get_db
from .rate_limit import get_login_rate_limiter, get_register_rate_limiter

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
router = APIRouter()

DEFAULT_TIMEZONE = "Asia/Shanghai"
MESSAGES_PAGE_SIZE = 50
REMEMBER_PASSWORD_COOKIE = "mailpulse_remember_credentials"
TASK_RUN_STAGES = {"sync", "summarize", "delivery", "complete"}


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


# ---------------------------------------------------------------------------
# 用户账号设置
# ---------------------------------------------------------------------------


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: User = Depends(authenticated_user)):
    return _render(request, "account.html", user=user, password_error=None, name_saved=False)


@router.post("/account", response_class=HTMLResponse)
def update_account(
    request: Request,
    display_name: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str | None = Form(None),
    user: User = Depends(authenticated_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    password_error = None
    if new_password:
        if current_password and authenticate(db, user.username, current_password) is None:
            password_error = "当前密码不正确"
        elif new_password != confirm_password:
            password_error = "两次输入的新密码不一致"
        else:
            try:
                set_password(user, new_password)
                db.add(
                    AuditLog(
                        actor_user_id=user.id,
                        action="password_change",
                        target_type="user",
                        target_id=str(user.id),
                    )
                )
            except ValueError as exc:
                password_error = str(exc)
    if display_name.strip():
        user.display_name = display_name.strip()
    db.commit()
    return _render(
        request,
        "account.html",
        user=user,
        password_error=password_error,
        name_saved=bool(display_name.strip()),
    )


# ---------------------------------------------------------------------------
# 概览（仪表盘）
# ---------------------------------------------------------------------------


def _task_run_label(db: Session, task: Task) -> dict:
    """Summarize the latest run of a task for list/dashboard cards."""
    job = db.scalar(
        select(JobRun).where(JobRun.task_id == task.id).order_by(JobRun.started_at.desc())
    )
    return {
        "status": job.status if job else None,
        "finished_at": job.finished_at if job else None,
        "stage": job.stage if job else None,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    tasks = list(
        db.scalars(select(Task).where(Task.user_id == user.id).order_by(Task.created_at.desc()))
    )
    mailboxes = list(db.scalars(select(Mailbox).where(Mailbox.user_id == user.id)))
    message_count = db.scalar(
        select(func.count())
        .select_from(CanonicalMessage)
        .where(CanonicalMessage.owner_user_id == user.id)
    )
    reports = list(
        db.scalars(
            select(Report)
            .where(Report.user_id == user.id)
            .order_by(Report.created_at.desc())
            .limit(5)
        )
    )
    task_cards = []
    for task in tasks:
        mailbox = db.get(Mailbox, task.mailbox_id)
        card = _task_run_label(db, task)
        card.update(
            {
                "task": task,
                "mailbox": mailbox,
                "next_run": (
                    next_fire_time(task)
                    if task.run_mode == "scheduled" and task.is_enabled
                    else None
                ),
                "target_count": sum(1 for t in task.delivery_targets if t.is_enabled),
            }
        )
        task_cards.append(card)
    return _render(
        request,
        "dashboard.html",
        user=user,
        tasks=tasks,
        task_cards=task_cards,
        mailboxes=mailboxes,
        message_count=message_count or 0,
        reports=reports,
        demo_available=not tasks,
    )


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


def _owned_task(db: Session, user: User, task_id: int) -> Task | None:
    return db.scalar(select(Task).where(Task.id == task_id, Task.user_id == user.id))


def _task_form_defaults() -> dict:
    return {
        "name": "",
        "run_mode": "manual",
        "schedule_type": "daily",
        "scheduled_time": "09:00",
        "weekdays": "mon-fri",
        "custom_cron": "",
        "timezone": DEFAULT_TIMEZONE,
        "lookback_hours": 24,
        "is_enabled": True,
    }


def _task_to_form(task: Task) -> dict:
    """Derive editable basic-form fields from a stored task (lossy for custom cron)."""
    form = {
        "name": task.name,
        "run_mode": task.run_mode,
        "timezone": task.timezone,
        "lookback_hours": task.lookback_hours,
        "is_enabled": task.is_enabled,
        "schedule_type": "custom",
        "scheduled_time": "09:00",
        "weekdays": "mon-fri",
        "custom_cron": task.cron_expression,
    }
    parts = task.cron_expression.split()
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


WEEKDAY_PRESETS = {"mon-fri", "mon-sun", "sat,sun", "mon,wed,fri"}

# 表单化规则编辑器支持的范围（高级结构仍可走 JSON 模式）
RULE_FORM_FIELDS = {
    "subject",
    "sender",
    "recipients",
    "cc",
    "body_text",
    "attachment_name",
    "local_labels",
}
RULE_FORM_OPERATORS = {
    "contains",
    "not_contains",
    "equals",
    "starts_with",
    "ends_with",
    "regex",
}
RULE_FIELD_LABELS = {
    "subject": "邮件标题",
    "sender": "发件人",
    "recipients": "收件人",
    "cc": "抄送",
    "body_text": "邮件正文",
    "attachment_name": "附件名称",
    "local_labels": "标签",
}
RULE_OPERATOR_LABELS = {
    "contains": "包含",
    "not_contains": "不包含",
    "equals": "等于",
    "starts_with": "开头是",
    "ends_with": "结尾是",
    "regex": "正则匹配",
}


def _conditions_to_definition(rows: list[dict]) -> dict:
    """Assemble a form-style condition list into a rule DSL node (AND)."""
    nodes = [{"kind": "condition", **row} for row in rows]
    if len(nodes) == 1:
        return nodes[0]
    return {"kind": "group", "operator": "and", "children": nodes}


def _definition_to_form_rows(definition: dict) -> list[dict] | None:
    """Convert a stored DSL back to editable form rows, or None when the
    structure cannot be expressed by the simple form (use JSON mode)."""
    if definition.get("kind") == "condition":
        return [
            {
                "field": definition.get("field", ""),
                "operator": definition.get("operator", "contains"),
                "value": str(definition.get("value") or ""),
            }
        ]
    if definition.get("kind") == "group" and definition.get("operator") == "and":
        rows = []
        for child in definition.get("children", []):
            if child.get("kind") != "condition":
                return None
            rows.append(
                {
                    "field": child.get("field", ""),
                    "operator": child.get("operator", "contains"),
                    "value": str(child.get("value") or ""),
                }
            )
        return rows or None
    return None


def _rule_summary(definition: dict) -> str:
    """Human-readable summary of a rule definition for list display."""
    rows = _definition_to_form_rows(definition)
    if rows is None:
        return "高级规则（JSON 结构）"
    parts = []
    for row in rows:
        field = RULE_FIELD_LABELS.get(row["field"], row["field"])
        operator = RULE_OPERATOR_LABELS.get(row["operator"], row["operator"])
        parts.append(f"{field} {operator} {row['value']}")
    return " 且 ".join(parts)


def _parse_rules_json(value: str) -> list[dict]:
    """Parse the wizard's rules_json payload into validated rule drafts."""
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("规则数据格式无效") from exc
    if not isinstance(parsed, list):
        raise ValueError("规则数据格式无效")
    drafts = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 条规则格式无效")
        name = str(item.get("name") or "").strip() or f"规则 {index + 1}"
        conditions = item.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise ValueError(f"规则「{name}」至少需要一个筛选条件")
        rows = []
        for condition in conditions:
            if not isinstance(condition, dict):
                raise ValueError(f"规则「{name}」的条件格式无效")
            field = str(condition.get("field") or "")
            operator = str(condition.get("operator") or "")
            value = str(condition.get("value") or "").strip()
            if field not in RULE_FORM_FIELDS:
                raise ValueError(f"规则「{name}」包含不支持的字段")
            if operator not in RULE_FORM_OPERATORS:
                raise ValueError(f"规则「{name}」包含不支持的操作符")
            if not value:
                raise ValueError(f"规则「{name}」的条件值不能为空")
            rows.append({"field": field, "operator": operator, "value": value})
        drafts.append(
            {
                "name": name,
                "conditions": rows,
                "definition": _conditions_to_definition(rows),
            }
        )
    return drafts


def _parse_targets_json(value: str) -> list[str]:
    """Parse the wizard's targets_json payload into normalized destinations."""
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("投递渠道数据格式无效") from exc
    if not isinstance(parsed, list):
        raise ValueError("投递渠道数据格式无效")
    targets = []
    for item in parsed:
        destination = str(item or "").strip().lower()
        if not destination:
            continue
        if "@" not in destination:
            raise ValueError(f"投递邮箱「{destination}」格式无效")
        if destination in targets:
            raise ValueError(f"投递邮箱「{destination}」已重复")
        targets.append(destination)
    return targets


def _collect_task_form(
    name: str,
    run_mode: str,
    schedule_type: str,
    scheduled_time: str,
    weekdays: str,
    custom_cron: str,
    timezone: str,
    lookback_hours: int,
    is_enabled: bool,
) -> dict:
    """Validate task basic form inputs; raises ValueError with a user-facing reason."""
    if run_mode not in {"manual", "scheduled"}:
        raise ValueError("执行方式必须是手动或定时")
    if not 1 <= lookback_hours <= 24 * 31:
        raise ValueError("回看时间必须在 1 到 744 小时之间")
    cron_expression = "0 9 * * 1-5"
    if run_mode == "scheduled":
        if schedule_type not in {"daily", "weekly", "custom"}:
            raise ValueError("任务计划类型无效")
        cron_expression = build_cron_expression(
            schedule_type, scheduled_time, weekdays, custom_cron
        )
    validate_schedule(cron_expression, timezone.strip())
    return {
        "name": name.strip() or "新任务",
        "run_mode": run_mode,
        "cron_expression": cron_expression,
        "timezone": timezone.strip(),
        "lookback_hours": lookback_hours,
        "is_enabled": is_enabled,
    }


def _mailbox_form_values(mailbox: Mailbox | None) -> dict:
    return {
        "name": mailbox.name if mailbox else "收件邮箱",
        "email_address": mailbox.email_address if mailbox else "",
        "imap_host": mailbox.imap_host if mailbox else "",
        "imap_port": mailbox.imap_port if mailbox else 993,
        "username": mailbox.username if mailbox else "",
        "password": "",
        "smtp_host": mailbox.smtp_host if mailbox else "",
        "smtp_port": mailbox.smtp_port if mailbox else 465,
        "folder": mailbox.folder if mailbox else "INBOX",
    }


def _render_task_page(
    request: Request,
    user: User,
    db: Session,
    task: Task,
    *,
    error: str | None = None,
    notice: str | None = None,
    form: dict | None = None,
    mailbox_form: dict | None = None,
    editing_rule: RuleSet | None = None,
    rule_form: dict | None = None,
    tested: bool = False,
    tested_smtp: bool = False,
    sync_failed: bool = False,
):
    mailbox = db.get(Mailbox, task.mailbox_id)
    rules = list(
        db.scalars(
            select(RuleSet)
            .where(RuleSet.task_id == task.id)
            .order_by(RuleSet.priority.asc(), RuleSet.id.asc())
        )
    )
    rule_views = []
    for index, item in enumerate(rules):
        rule_views.append(
            {
                "rule": item,
                "summary": _rule_summary(item.definition),
                "first": index == 0,
                "last": index == len(rules) - 1,
            }
        )
    targets = list(task.delivery_targets)
    jobs = list(
        db.scalars(
            select(JobRun)
            .where(JobRun.task_id == task.id)
            .order_by(JobRun.started_at.desc())
            .limit(10)
        )
    )
    reports = list(
        db.scalars(
            select(Report)
            .where(Report.task_id == task.id)
            .order_by(Report.created_at.desc())
            .limit(10)
        )
    )
    other_mailboxes = list(
        db.scalars(
            select(Mailbox)
            .where(Mailbox.user_id == user.id, Mailbox.id != task.mailbox_id)
            .order_by(Mailbox.created_at.desc())
        )
    )
    if editing_rule is not None and rule_form is None:
        rows = _definition_to_form_rows(editing_rule.definition)
        rule_form = {
            "name": editing_rule.name,
            "priority": editing_rule.priority,
            "mode": "form" if rows is not None else "json",
            "definition": json.dumps(editing_rule.definition, ensure_ascii=False, indent=2),
            "conditions": rows or [],
        }
    elif rule_form is not None and "conditions" not in rule_form:
        rule_form = {
            **rule_form,
            "mode": rule_form.get("mode", "form"),
            "conditions": rule_form.get("conditions", []),
        }
    return _render(
        request,
        "task_detail.html",
        user=user,
        task=task,
        mailbox=mailbox,
        mailbox_form=mailbox_form if mailbox_form is not None else _mailbox_form_values(mailbox),
        rules=rules,
        rule_views=rule_views,
        targets=targets,
        jobs=jobs,
        reports=reports,
        other_mailboxes=other_mailboxes,
        form=form if form is not None else _task_to_form(task),
        editing_rule=editing_rule,
        rule_form=rule_form
        or {
            "name": "",
            "priority": 100,
            "mode": "form",
            "definition": "",
            "conditions": [],
        },
        rule_default_definition=json.dumps(MATCH_ALL, ensure_ascii=False, indent=2),
        rule_fields=RULE_FORM_FIELDS,
        rule_operators=RULE_FORM_OPERATORS,
        rule_field_labels=RULE_FIELD_LABELS,
        rule_operator_labels=RULE_OPERATOR_LABELS,
        error=error,
        notice=notice,
        tested=tested,
        tested_smtp=tested_smtp,
        sync_failed=sync_failed,
    )


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    tasks = list(
        db.scalars(select(Task).where(Task.user_id == user.id).order_by(Task.created_at.desc()))
    )
    cards = []
    for task in tasks:
        mailbox = db.get(Mailbox, task.mailbox_id)
        card = _task_run_label(db, task)
        card.update(
            {
                "task": task,
                "mailbox": mailbox,
                "next_run": (
                    next_fire_time(task)
                    if task.run_mode == "scheduled" and task.is_enabled
                    else None
                ),
                "enabled_targets": [t for t in task.delivery_targets if t.is_enabled],
            }
        )
        cards.append(card)
    return _render(request, "tasks.html", user=user, cards=cards, error=None)


@router.get("/tasks/new", response_class=HTMLResponse)
def task_new_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    return _render_task_new_page(
        request,
        user,
        db,
        form=_task_form_defaults(),
        mailbox_form=_mailbox_form_values(None),
        rules_form=[],
        targets_form=[],
        error=None,
        notice=None,
    )


def _render_task_new_page(
    request: Request,
    user: User,
    db: Session,
    *,
    form: dict,
    mailbox_form: dict,
    rules_form: list[dict],
    targets_form: list[str],
    error: str | None,
    notice: str | None = None,
):
    mailboxes = list(
        db.scalars(
            select(Mailbox)
            .where(Mailbox.user_id == user.id)
            .order_by(Mailbox.created_at.desc())
        )
    )
    return _render(
        request,
        "task_new.html",
        user=user,
        form=form,
        mailbox_form=mailbox_form,
        rules_form=rules_form,
        targets_form=targets_form,
        mailboxes=mailboxes,
        rule_fields=RULE_FORM_FIELDS,
        rule_operators=RULE_FORM_OPERATORS,
        rule_field_labels=RULE_FIELD_LABELS,
        rule_operator_labels=RULE_OPERATOR_LABELS,
        error=error,
        notice=notice,
    )


def _wizard_mailbox_values(
    mailbox_name: str,
    email_address: str,
    imap_host: str,
    imap_port: int,
    username: str,
    password: str,
    smtp_host: str,
    smtp_port: int,
    folder: str,
) -> dict:
    return {
        "name": mailbox_name,
        "email_address": email_address,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "username": username,
        "password": password,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "folder": folder,
    }


def _wizard_form_values(
    name: str,
    run_mode: str,
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
        "run_mode": run_mode,
        "schedule_type": schedule_type,
        "scheduled_time": scheduled_time,
        "weekdays": weekdays,
        "custom_cron": custom_cron,
        "timezone": timezone,
        "lookback_hours": lookback_hours,
        "is_enabled": is_enabled,
    }


@router.post("/tasks/new/test-imap", response_class=HTMLResponse)
def test_new_task_imap(
    request: Request,
    name: str = Form(""),
    run_mode: str = Form("manual"),
    schedule_type: str = Form("daily"),
    scheduled_time: str = Form("09:00"),
    weekdays: str = Form("mon-fri"),
    custom_cron: str = Form(""),
    timezone: str = Form(DEFAULT_TIMEZONE),
    lookback_hours: int = Form(24),
    is_enabled: bool = Form(True),
    copy_from: int | None = Form(None),
    mailbox_name: str = Form("收件邮箱"),
    email_address: str = Form(""),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    username: str = Form(""),
    password: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(465),
    folder: str = Form("INBOX"),
    rules_json: str = Form("[]"),
    targets_json: str = Form("[]"),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    form = _wizard_form_values(
        name, run_mode, schedule_type, scheduled_time, weekdays,
        custom_cron, timezone, lookback_hours, is_enabled,
    )
    mailbox_form = _wizard_mailbox_values(
        mailbox_name, email_address, imap_host, imap_port, username,
        password, smtp_host, smtp_port, folder,
    )
    try:
        rules_form = _parse_rules_json(rules_json)
        targets_form = _parse_targets_json(targets_json)
    except ValueError as exc:
        return _render_task_new_page(
            request, user, db, form=form, mailbox_form=mailbox_form,
            rules_form=_rules_json_to_drafts(rules_json),
            targets_form=_targets_json_to_list(targets_json),
            error=str(exc),
        )
    connection = _draft_mailbox_connection(
        db, user, copy_from, email_address, imap_host, imap_port, username,
        password, folder, smtp_host, smtp_port,
    )
    try:
        IMAPConnector(connection).test_connection()
        notice = "IMAP 连接验证成功。"
    except Exception as exc:
        notice = None
        return _render_task_new_page(
            request, user, db, form=form, mailbox_form=mailbox_form,
            rules_form=rules_form, targets_form=targets_form,
            error=f"连接测试失败：{error_message(exc)}",
        )
    return _render_task_new_page(
        request, user, db, form=form, mailbox_form=mailbox_form,
        rules_form=rules_form, targets_form=targets_form,
        error=None, notice=notice,
    )


@router.post("/tasks/new/test-smtp", response_class=HTMLResponse)
def test_new_task_smtp(
    request: Request,
    name: str = Form(""),
    run_mode: str = Form("manual"),
    schedule_type: str = Form("daily"),
    scheduled_time: str = Form("09:00"),
    weekdays: str = Form("mon-fri"),
    custom_cron: str = Form(""),
    timezone: str = Form(DEFAULT_TIMEZONE),
    lookback_hours: int = Form(24),
    is_enabled: bool = Form(True),
    copy_from: int | None = Form(None),
    mailbox_name: str = Form("收件邮箱"),
    email_address: str = Form(""),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    username: str = Form(""),
    password: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(465),
    folder: str = Form("INBOX"),
    rules_json: str = Form("[]"),
    targets_json: str = Form("[]"),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    form = _wizard_form_values(
        name, run_mode, schedule_type, scheduled_time, weekdays,
        custom_cron, timezone, lookback_hours, is_enabled,
    )
    mailbox_form = _wizard_mailbox_values(
        mailbox_name, email_address, imap_host, imap_port, username,
        password, smtp_host, smtp_port, folder,
    )
    try:
        rules_form = _parse_rules_json(rules_json)
        targets_form = _parse_targets_json(targets_json)
    except ValueError as exc:
        return _render_task_new_page(
            request, user, db, form=form, mailbox_form=mailbox_form,
            rules_form=_rules_json_to_drafts(rules_json),
            targets_form=_targets_json_to_list(targets_json),
            error=str(exc),
        )
    effective_smtp_host = smtp_host
    if copy_from is not None:
        source = db.scalar(
            select(Mailbox).where(Mailbox.id == copy_from, Mailbox.user_id == user.id)
        )
        if source is None:
            return _render_task_new_page(
                request, user, db, form=form, mailbox_form=mailbox_form,
                rules_form=rules_form, targets_form=targets_form,
                error="复制的邮箱不存在或不属于当前用户",
            )
        if not effective_smtp_host:
            effective_smtp_host = source.smtp_host
            mailbox_form = {
                **mailbox_form,
                "smtp_host": source.smtp_host,
                "smtp_port": source.smtp_port,
            }
    if not effective_smtp_host:
        return _render_task_new_page(
            request, user, db, form=form, mailbox_form=mailbox_form,
            rules_form=rules_form, targets_form=targets_form,
            error="SMTP 主机未配置，无法验证连接",
        )
    effective_password = password or _draft_mailbox_password(db, user, copy_from)
    try:
        SMTPDeliveryProvider(
            SMTPConfig(
                host=effective_smtp_host,
                port=smtp_port,
                username=username,
                password=effective_password,
                use_tls=True,
            )
        ).test_connection()
        notice = "SMTP 连接验证成功。"
    except Exception as exc:
        notice = None
        return _render_task_new_page(
            request, user, db, form=form, mailbox_form=mailbox_form,
            rules_form=rules_form, targets_form=targets_form,
            error=f"SMTP 连接测试失败：{error_message(exc)}",
        )
    return _render_task_new_page(
        request, user, db, form=form, mailbox_form=mailbox_form,
        rules_form=rules_form, targets_form=targets_form,
        error=None, notice=notice,
    )


def _rules_json_to_drafts(value: str) -> list[dict]:
    try:
        return _parse_rules_json(value)
    except ValueError:
        return []


def _targets_json_to_list(value: str) -> list[str]:
    try:
        return _parse_targets_json(value)
    except ValueError:
        return []


def _draft_mailbox_password(db: Session, user: User, copy_from: int | None) -> str:
    if copy_from is None:
        return ""
    source = db.scalar(
        select(Mailbox).where(Mailbox.id == copy_from, Mailbox.user_id == user.id)
    )
    if source is None:
        return ""
    return decrypt_secret(source.credential_encrypted, get_settings())


def _draft_mailbox_connection(
    db: Session,
    user: User,
    copy_from: int | None,
    email_address: str,
    imap_host: str,
    imap_port: int,
    username: str,
    password: str,
    folder: str,
    smtp_host: str,
    smtp_port: int,
) -> MailboxConnection:
    if copy_from is not None:
        source = db.scalar(
            select(Mailbox).where(Mailbox.id == copy_from, Mailbox.user_id == user.id)
        )
        if source is not None:
            return MailboxConnection(
                host=source.imap_host,
                port=source.imap_port,
                username=source.username,
                password=decrypt_secret(source.credential_encrypted, get_settings()),
                tls=source.imap_tls,
                folder=source.folder,
            )
    return MailboxConnection(
        host=imap_host.strip(),
        port=imap_port,
        username=username.strip(),
        password=password,
        tls=True,
        folder=folder.strip() or "INBOX",
    )


@router.post("/tasks", response_class=HTMLResponse)
def create_task(
    request: Request,
    name: str = Form(""),
    run_mode: str = Form("manual"),
    schedule_type: str = Form("daily"),
    scheduled_time: str = Form("09:00"),
    weekdays: str = Form("mon-fri"),
    custom_cron: str = Form(""),
    timezone: str = Form(DEFAULT_TIMEZONE),
    lookback_hours: int = Form(24),
    is_enabled: bool = Form(True),
    copy_from: int | None = Form(None),
    mailbox_name: str = Form("收件邮箱"),
    email_address: str = Form(""),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    username: str = Form(""),
    password: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(465),
    folder: str = Form("INBOX"),
    rules_json: str = Form("[]"),
    targets_json: str = Form("[]"),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    submitted_form = _wizard_form_values(
        name, run_mode, schedule_type, scheduled_time, weekdays,
        custom_cron, timezone, lookback_hours, is_enabled,
    )
    submitted_mailbox = _wizard_mailbox_values(
        mailbox_name, email_address, imap_host, imap_port, username,
        password, smtp_host, smtp_port, folder,
    )
    try:
        rules_json_parsed = _parse_rules_json(rules_json)
        targets_json_parsed = _parse_targets_json(targets_json)
        values = _collect_task_form(
            name, run_mode, schedule_type, scheduled_time, weekdays,
            custom_cron, timezone, lookback_hours, is_enabled,
        )
        if copy_from is not None:
            source = db.scalar(
                select(Mailbox).where(Mailbox.id == copy_from, Mailbox.user_id == user.id)
            )
            if source is None:
                raise ValueError("复制的邮箱不存在或不属于当前用户")
            mailbox = Mailbox(
                user_id=user.id,
                name=source.name,
                email_address=source.email_address,
                imap_host=source.imap_host,
                imap_port=source.imap_port,
                smtp_host=source.smtp_host,
                smtp_port=source.smtp_port,
                username=source.username,
                credential_encrypted=source.credential_encrypted,
                folder=source.folder,
            )
        else:
            if not password:
                raise ValueError("首次配置邮箱必须填写邮箱密码")
            mailbox = Mailbox(
                user_id=user.id,
                name=mailbox_name.strip() or "收件邮箱",
                email_address=email_address.strip(),
                imap_host=imap_host.strip(),
                imap_port=imap_port,
                username=username.strip(),
                credential_encrypted=encrypt_secret(password),
                smtp_host=smtp_host.strip(),
                smtp_port=smtp_port,
                folder=folder.strip() or "INBOX",
            )
        db.add(mailbox)
        db.flush()
        task = Task(user_id=user.id, mailbox_id=mailbox.id, **values)
        db.add(task)
        db.flush()
        for index, rule in enumerate(rules_json_parsed):
            db.add(
                RuleSet(
                    task_id=task.id,
                    name=rule["name"],
                    definition=rule["definition"],
                    priority=100 + index * 10,
                )
            )
        for destination in targets_json_parsed:
            db.add(TaskDeliveryTarget(task_id=task.id, channel="smtp", destination=destination))
        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="task_create",
                target_type="task",
                target_id=str(task.id),
                metadata_json={
                    "run_mode": values["run_mode"],
                    "rules": len(rules_json_parsed),
                    "targets": len(targets_json_parsed),
                },
            )
        )
        db.commit()
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)
    except ValueError as exc:
        db.rollback()
        return _render_task_new_page(
            request,
            user,
            db,
            form=submitted_form,
            mailbox_form=submitted_mailbox,
            rules_form=_rules_json_to_drafts(rules_json),
            targets_form=_targets_json_to_list(targets_json),
            error=f"任务配置无效：{exc}",
        )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail_page(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    notice = None
    if request.query_params.get("saved"):
        notice = "配置已保存。"
    elif request.query_params.get("notice") == "test-sent":
        notice = "测试邮件已发送，请检查收件箱。"
    elif request.query_params.get("run") == "failed":
        notice = "任务运行失败，请查看运行记录中的错误信息。"
    elif request.query_params.get("sync") == "failed":
        notice = "邮箱同步失败，请查看同步状态。"
    elif request.query_params.get("sync") == "ok":
        notice = "邮箱同步完成。"
    return _render_task_page(request, user, db, task, notice=notice)


@router.post("/tasks/{task_id}/basic")
def update_task_basic(
    request: Request,
    task_id: int,
    name: str = Form(""),
    run_mode: str = Form("manual"),
    schedule_type: str = Form("daily"),
    scheduled_time: str = Form("09:00"),
    weekdays: str = Form("mon-fri"),
    custom_cron: str = Form(""),
    timezone: str = Form(DEFAULT_TIMEZONE),
    lookback_hours: int = Form(24),
    is_enabled: bool = Form(True),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    try:
        values = _collect_task_form(
            name, run_mode, schedule_type, scheduled_time, weekdays,
            custom_cron, timezone, lookback_hours, is_enabled,
        )
        for key, value in values.items():
            setattr(task, key, value)
        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="task_update",
                target_type="task",
                target_id=str(task.id),
            )
        )
        db.commit()
        return RedirectResponse(f"/tasks/{task.id}?saved=1", status_code=303)
    except ValueError as exc:
        db.rollback()
        form = {
            "name": name,
            "run_mode": run_mode,
            "schedule_type": schedule_type,
            "scheduled_time": scheduled_time,
            "weekdays": weekdays,
            "custom_cron": custom_cron,
            "timezone": timezone,
            "lookback_hours": lookback_hours,
            "is_enabled": is_enabled,
        }
        return _render_task_page(request, user, db, task, error=f"任务配置无效：{exc}", form=form)


@router.post("/tasks/{task_id}/mailbox")
def update_task_mailbox(
    request: Request,
    task_id: int,
    mailbox_name: str = Form("收件邮箱"),
    email_address: str = Form(""),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    username: str = Form(""),
    password: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(465),
    folder: str = Form("INBOX"),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    mailbox = db.get(Mailbox, task.mailbox_id)
    mailbox.name = mailbox_name.strip() or "收件邮箱"
    mailbox.email_address = email_address.strip()
    mailbox.imap_host = imap_host.strip()
    mailbox.imap_port = imap_port
    mailbox.username = username.strip()
    mailbox.smtp_host = smtp_host.strip()
    mailbox.smtp_port = smtp_port
    mailbox.folder = folder.strip() or "INBOX"
    if password:
        mailbox.credential_encrypted = encrypt_secret(password)
    mailbox.sync_error = None
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}?saved=1", status_code=303)


def _render_mailbox_test(
    request: Request,
    user: User,
    db: Session,
    task: Task,
    error: str | None,
    tested: bool = False,
    tested_smtp: bool = False,
):
    return _render_task_page(
        request, user, db, task, error=error, tested=tested, tested_smtp=tested_smtp
    )


@router.post("/tasks/{task_id}/mailbox/test")
def test_task_mailbox(
    request: Request,
    task_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    mailbox = db.get(Mailbox, task.mailbox_id)
    try:
        _build_imap_connector(mailbox, get_settings()).test_connection()
        return _render_mailbox_test(request, user, db, task, error=None, tested=True)
    except Exception as exc:
        return _render_mailbox_test(
            request, user, db, task, error=f"连接测试失败：{error_message(exc)}"
        )


@router.post("/tasks/{task_id}/mailbox/test-smtp")
def test_task_mailbox_smtp(
    request: Request,
    task_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    mailbox = db.get(Mailbox, task.mailbox_id)
    if not mailbox.smtp_host:
        return _render_mailbox_test(
            request, user, db, task, error="SMTP 主机未配置，无法验证连接"
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
        return _render_mailbox_test(request, user, db, task, error=None, tested_smtp=True)
    except Exception as exc:
        return _render_mailbox_test(
            request, user, db, task, error=f"SMTP 连接测试失败：{error_message(exc)}"
        )


@router.post("/tasks/{task_id}/sync")
def sync_task_mailbox(
    request: Request,
    task_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    mailbox = db.get(Mailbox, task.mailbox_id)
    try:
        connector = _build_imap_connector(mailbox, get_settings())
        MailSyncService(db, get_settings()).sync(mailbox, connector)
        mailbox.sync_error = None
        db.commit()
        return RedirectResponse(f"/tasks/{task.id}?sync=ok", status_code=303)
    except Exception as exc:
        db.rollback()
        mailbox = db.get(Mailbox, task.mailbox_id)
        if mailbox:
            mailbox.sync_error = error_message(exc, "邮箱同步失败")
            db.commit()
        return RedirectResponse(f"/tasks/{task.id}?sync=failed", status_code=303)


@router.post("/tasks/{task_id}/run")
def run_task(
    request: Request,
    task_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    run_key = f"manual:{task.id}:{uuid4().hex}"
    job = run_task_now(db, task, get_settings(), run_key, datetime.now(UTC))
    db.commit()
    if job.status == "success":
        report = db.scalar(
            select(Report).where(Report.task_id == task.id, Report.run_key == run_key)
        )
        if report is not None:
            return RedirectResponse(f"/reports/{report.id}", status_code=303)
        return RedirectResponse(f"/tasks/{task.id}?run=success", status_code=303)
    return RedirectResponse(f"/tasks/{task.id}?run=failed", status_code=303)


@router.post("/tasks/{task_id}/toggle")
def toggle_task(
    request: Request,
    task_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    task.is_enabled = not task.is_enabled
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@router.post("/tasks/{task_id}/delete")
def delete_task(
    request: Request,
    task_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    mailbox_id = task.mailbox_id
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="task_delete",
            target_type="task",
            target_id=str(task.id),
        )
    )
    db.delete(task)
    # 邮箱按任务独立配置：没有其他任务引用时一并删除，避免残留孤儿邮箱。
    still_used = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.mailbox_id == mailbox_id, Task.id != task_id)
    )
    if not still_used:
        mailbox = db.get(Mailbox, mailbox_id)
        if mailbox is not None:
            db.delete(mailbox)
    db.commit()
    return RedirectResponse("/tasks", status_code=303)


# --- 任务筛选规则 ----------------------------------------------------------


def _owned_rule(db: Session, user: User, task: Task, rule_id: int) -> RuleSet | None:
    return db.scalar(
        select(RuleSet).where(RuleSet.id == rule_id, RuleSet.task_id == task.id)
    )


@router.post("/tasks/{task_id}/rules")
def create_task_rule(
    request: Request,
    task_id: int,
    name: str = Form(""),
    mode: str = Form("form"),
    definition: str = Form(""),
    rules_json: str = Form("[]"),
    priority: int = Form(100),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    try:
        parsed, rule_name, rows = _resolve_rule_submission(
            db, name, mode, definition, rules_json
        )
    except ValueError as exc:
        rows = []
        try:
            drafts = _parse_rules_json(rules_json)
            if drafts:
                rows = drafts[0]["conditions"]
        except ValueError:
            pass
        return _render_task_page(
            request,
            user,
            db,
            task,
            error=f"规则无效：{exc}",
            rule_form={
                "name": name,
                "priority": priority,
                "mode": mode,
                "definition": definition,
                "conditions": rows,
            },
        )
    db.add(
        RuleSet(
            task_id=task.id, name=rule_name,
            definition=parsed, priority=priority,
        )
    )
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}?saved=1", status_code=303)


def _resolve_rule_submission(
    db: Session, name: str, mode: str, definition: str, rules_json: str
) -> tuple[dict, str, list[dict]]:
    """Resolve a rule submission (form rows or JSON) into (dsl, name, rows)."""
    if mode == "json":
        try:
            parsed = json.loads(definition)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 格式无效") from exc
        RuleService(db).validate(parsed)
        rows = _definition_to_form_rows(parsed) or []
        return parsed, name.strip() or "未命名规则", rows
    drafts = _parse_rules_json(rules_json)
    if not drafts:
        raise ValueError("规则至少需要一个筛选条件")
    draft = drafts[0]
    return draft["definition"], draft["name"], draft["conditions"]


@router.get("/tasks/{task_id}/rules/{rule_id}/edit", response_class=HTMLResponse)
def edit_task_rule_page(
    request: Request,
    task_id: int,
    rule_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    rule_set = _owned_rule(db, user, task, rule_id)
    if rule_set is None:
        return HTMLResponse("规则不存在", status_code=404)
    rows = _definition_to_form_rows(rule_set.definition)
    return _render_task_page(
        request,
        user,
        db,
        task,
        editing_rule=rule_set,
        rule_form={
            "name": rule_set.name,
            "priority": rule_set.priority,
            "mode": "form" if rows is not None else "json",
            "definition": json.dumps(rule_set.definition, ensure_ascii=False, indent=2),
            "conditions": rows or [],
        },
    )


@router.post("/tasks/{task_id}/rules/{rule_id}/edit")
def update_task_rule(
    request: Request,
    task_id: int,
    rule_id: int,
    name: str = Form(""),
    mode: str = Form("form"),
    definition: str = Form(""),
    rules_json: str = Form("[]"),
    priority: int = Form(100),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    rule_set = _owned_rule(db, user, task, rule_id)
    if rule_set is None:
        return HTMLResponse("规则不存在", status_code=404)
    try:
        parsed, rule_name, rows = _resolve_rule_submission(
            db, name, mode, definition, rules_json
        )
        rule_set.name = rule_name
        rule_set.priority = priority
        rule_set.definition = parsed
        db.commit()
        return RedirectResponse(f"/tasks/{task.id}?saved=1", status_code=303)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        rows: list[dict] = []
        try:
            drafts = _parse_rules_json(rules_json)
            if drafts:
                rows = drafts[0]["conditions"]
        except ValueError:
            pass
        return _render_task_page(
            request,
            user,
            db,
            task,
            error=f"规则无效：{exc}",
            editing_rule=rule_set,
            rule_form={
                "name": name,
                "priority": priority,
                "mode": mode,
                "definition": definition,
                "conditions": rows,
            },
        )


@router.post("/tasks/{task_id}/rules/{rule_id}/move")
def move_task_rule(
    request: Request,
    task_id: int,
    rule_id: int,
    direction: str = Form("up"),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    rules = list(
        db.scalars(
            select(RuleSet)
            .where(RuleSet.task_id == task.id)
            .order_by(RuleSet.priority.asc(), RuleSet.id.asc())
        )
    )
    index = next((i for i, item in enumerate(rules) if item.id == rule_id), None)
    if index is None:
        return HTMLResponse("规则不存在", status_code=404)
    target_index = index - 1 if direction == "up" else index + 1
    if 0 <= target_index < len(rules):
        rules[index].priority, rules[target_index].priority = (
            rules[target_index].priority,
            rules[index].priority,
        )
        db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@router.post("/tasks/{task_id}/rules/{rule_id}/toggle")
def toggle_task_rule(
    request: Request,
    task_id: int,
    rule_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    rule_set = _owned_rule(db, user, task, rule_id)
    if rule_set is None:
        return HTMLResponse("规则不存在", status_code=404)
    rule_set.is_enabled = not rule_set.is_enabled
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@router.post("/tasks/{task_id}/rules/{rule_id}/delete")
def delete_task_rule(
    request: Request,
    task_id: int,
    rule_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    rule_set = _owned_rule(db, user, task, rule_id)
    if rule_set is None:
        return HTMLResponse("规则不存在", status_code=404)
    db.delete(rule_set)
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


# --- 任务投递渠道 ----------------------------------------------------------


@router.post("/tasks/{task_id}/targets")
def add_task_target(
    request: Request,
    task_id: int,
    destination: str = Form(...),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    destination = destination.strip().lower()
    if "@" not in destination:
        return _render_task_page(request, user, db, task, error="投递邮箱格式无效")
    if any(item.destination == destination for item in task.delivery_targets):
        return _render_task_page(request, user, db, task, error="该投递邮箱已存在")
    db.add(TaskDeliveryTarget(task_id=task.id, channel="smtp", destination=destination))
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}?saved=1", status_code=303)


@router.post("/tasks/{task_id}/targets/{target_id}/edit")
def edit_task_target(
    request: Request,
    task_id: int,
    target_id: int,
    destination: str = Form(...),
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    target = db.scalar(
        select(TaskDeliveryTarget).where(
            TaskDeliveryTarget.id == target_id, TaskDeliveryTarget.task_id == task.id
        )
    )
    if target is None:
        return HTMLResponse("投递目标不存在", status_code=404)
    destination = destination.strip().lower()
    if "@" not in destination:
        return _render_task_page(request, user, db, task, error="投递邮箱格式无效")
    duplicate = db.scalar(
        select(TaskDeliveryTarget).where(
            TaskDeliveryTarget.task_id == task.id,
            TaskDeliveryTarget.destination == destination,
            TaskDeliveryTarget.id != target_id,
        )
    )
    if duplicate is not None:
        return _render_task_page(request, user, db, task, error="该投递邮箱已存在")
    target.destination = destination
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}?saved=1", status_code=303)


@router.post("/tasks/{task_id}/targets/{target_id}/test")
def test_task_target(
    request: Request,
    task_id: int,
    target_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    target = db.scalar(
        select(TaskDeliveryTarget).where(
            TaskDeliveryTarget.id == target_id, TaskDeliveryTarget.task_id == task.id
        )
    )
    if target is None:
        return HTMLResponse("投递目标不存在", status_code=404)
    mailbox = db.get(Mailbox, task.mailbox_id)
    if not mailbox.smtp_host:
        return _render_task_page(
            request,
            user,
            db,
            task,
            error="发送测试邮件需要先在该任务「收件邮箱」中配置 SMTP 发件信息",
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
        ).send(
            mailbox.email_address,
            target.destination,
            "MailPulse 投递渠道测试",
            (
                "这是一封来自 MailPulse 的测试邮件。\n\n"
                f"任务：{task.name}\n"
                f"投递地址：{target.destination}\n\n"
                "如果收到此邮件，说明该任务的 SMTP 发件配置正常。"
            ),
        )
    except Exception as exc:
        return _render_task_page(
            request,
            user,
            db,
            task,
            error=f"测试邮件发送失败：{error_message(exc)}",
        )
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="delivery_test",
            target_type="delivery_target",
            target_id=str(target.id),
            metadata_json={"destination": target.destination},
        )
    )
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}?notice=test-sent", status_code=303)


@router.post("/tasks/{task_id}/targets/{target_id}/toggle")
def toggle_task_target(
    request: Request,
    task_id: int,
    target_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    target = db.scalar(
        select(TaskDeliveryTarget).where(
            TaskDeliveryTarget.id == target_id, TaskDeliveryTarget.task_id == task.id
        )
    )
    if target is None:
        return HTMLResponse("投递目标不存在", status_code=404)
    target.is_enabled = not target.is_enabled
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@router.post("/tasks/{task_id}/targets/{target_id}/delete")
def delete_task_target(
    request: Request,
    task_id: int,
    target_id: int,
    csrf_token: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    task = _owned_task(db, user, task_id)
    if task is None:
        return HTMLResponse("任务不存在", status_code=404)
    target = db.scalar(
        select(TaskDeliveryTarget).where(
            TaskDeliveryTarget.id == target_id, TaskDeliveryTarget.task_id == task.id
        )
    )
    if target is None:
        return HTMLResponse("投递目标不存在", status_code=404)
    db.delete(target)
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


# ---------------------------------------------------------------------------
# 邮件（全局搜索与查看，范围为所有任务邮箱同步的全部邮件）
# ---------------------------------------------------------------------------


@router.get("/messages", response_class=HTMLResponse)
def messages_page(
    request: Request,
    q: str = "",
    status: str = "",
    mailbox_id: int | None = None,
    page: int = 1,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    status = status if status in {"unprocessed", "processed", "starred"} else ""
    mailboxes = list(db.scalars(select(Mailbox).where(Mailbox.user_id == user.id)))
    if mailbox_id is not None:
        owned_ids = {item.id for item in mailboxes}
        mailbox_id = mailbox_id if mailbox_id in owned_ids else None
    page = max(1, page)
    service = SearchService(db)
    total = service.count(user.id, q, status, mailbox_id=mailbox_id)
    pages = max(1, ceil(total / MESSAGES_PAGE_SIZE))
    page = min(page, pages)
    messages = service.search(
        user.id,
        q,
        status=status,
        mailbox_id=mailbox_id,
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
        mailbox_id=mailbox_id,
        mailboxes=mailboxes,
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


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    task_id: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    tasks = list(
        db.scalars(select(Task).where(Task.user_id == user.id).order_by(Task.created_at.desc()))
    )
    statement = (
        select(Report).where(Report.user_id == user.id).order_by(Report.created_at.desc())
    )
    if task_id is not None:
        owned_ids = {item.id for item in tasks}
        if task_id not in owned_ids:
            task_id = None
    if task_id is not None:
        statement = statement.where(Report.task_id == task_id)
    reports = list(db.scalars(statement.limit(100)))
    return _render(
        request, "reports.html", user=user, reports=reports, tasks=tasks, task_id=task_id,
        error=None,
    )


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
    task = db.get(Task, report.task_id) if report.task_id else None
    targets = [item for item in task.delivery_targets if item.is_enabled] if task else []
    return _render(
        request,
        "report_detail.html",
        user=user,
        report=report,
        task=task,
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


# ---------------------------------------------------------------------------
# 管理员控制台
# ---------------------------------------------------------------------------


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
