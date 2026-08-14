from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import authenticate
from ..config import get_settings
from ..demo import seed_demo
from ..models import AuditLog, CanonicalMessage, JobRun, Mailbox, Report, User
from ..report_service import ReportService
from ..search import SearchService
from ..security import encrypt_secret
from .csrf import get_csrf_token, validate_csrf
from .deps import admin_user, current_user, get_db

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
    user = authenticate(db, email, password)
    if user is None:
        return _render(request, "login.html", error="账号或密码错误")
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
        error=None,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    mailbox = db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))
    return _render(request, "settings.html", user=user, mailbox=mailbox, error=None, saved=False)


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
    return _render(request, "settings.html", user=user, mailbox=mailbox, error=None, saved=True)


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
    service.generate_for_user(user, use_demo_provider=use_demo_provider)
    db.commit()
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
    return _render(request, "reports.html", user=user, reports=reports)


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
    return _render(request, "report_detail.html", user=user, report=report)


@router.get("/messages", response_class=HTMLResponse)
def messages_page(
    request: Request,
    q: str = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    messages = SearchService(db).search(user.id, q)
    return _render(request, "messages.html", user=user, messages=messages, query=q)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    users = list(db.scalars(select(User).order_by(User.created_at.desc())))
    jobs = list(db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(20)))
    return _render(request, "admin.html", user=user, users=users, jobs=jobs)
