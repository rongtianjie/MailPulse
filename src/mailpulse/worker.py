from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import select

from .config import Settings, get_settings
from .db import build_session_factory
from .delivery import ReportDeliveryService
from .errors import error_message
from .mail.connectors import IMAPConnector
from .mail.sync import MailSyncService
from .mail.types import MailboxConnection
from .models import Delivery, JobRun, Mailbox, Task, User
from .report_service import ReportService
from .security import decrypt_secret


def run_due_tasks(settings: Settings | None = None) -> int:
    """Run every enabled scheduled task whose cron slot has been reached."""
    settings = settings or get_settings()
    session = build_session_factory(settings)()
    completed = 0
    try:
        now = datetime.now(UTC)
        tasks = list(
            session.scalars(
                select(Task).where(Task.is_enabled.is_(True), Task.run_mode == "scheduled")
            )
        )
        for task in tasks:
            scheduled_fire = _due_fire_time(task, now)
            if scheduled_fire is None:
                continue
            try:
                run_key = f"schedule:{task.id}:{scheduled_fire.astimezone(UTC).isoformat()}"
                job = run_task_now(session, task, settings, run_key, now)
                completed += int(job.status == "success")
            except Exception as exc:
                session.rollback()
                logger.error("task {} failed: {}", task.id, type(exc).__name__)
        return completed
    finally:
        session.close()


def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_due_tasks,
        "interval",
        seconds=30,
        kwargs={"settings": settings},
        id="mailpulse-task-poll",
        max_instances=1,
        coalesce=True,
    )
    run_due_tasks(settings)
    scheduler.start()


def _is_due(task: Task, now: datetime) -> bool:
    return _due_fire_time(task, now) is not None


def _due_fire_time(task: Task, now: datetime) -> datetime | None:
    if task.run_mode != "scheduled":
        return None
    try:
        tz = ZoneInfo(task.timezone)
    except ZoneInfoNotFoundError:
        return None
    local_now = now.astimezone(tz)
    try:
        trigger = CronTrigger.from_crontab(task.cron_expression, timezone=tz)
    except (TypeError, ValueError):
        return None
    if task.last_run_at is None:
        # A newly created task can run only its current day's reached slot.
        # Historical catch-up is driven by last_run_at after the first success.
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        candidate = trigger.get_next_fire_time(None, day_start - timedelta(minutes=1))
        if (
            candidate is not None
            and candidate.date() == local_now.date()
            and candidate <= local_now
        ):
            return candidate
        return None
    previous = _as_aware(task.last_run_at).astimezone(tz)
    next_fire = trigger.get_next_fire_time(previous, local_now)
    return next_fire if next_fire is not None and next_fire <= local_now else None


def next_fire_time(task: Task, after: datetime | None = None) -> datetime | None:
    """Compute the next cron fire time of a scheduled task (for display)."""
    if task.run_mode != "scheduled":
        return None
    try:
        tz = ZoneInfo(task.timezone)
        trigger = CronTrigger.from_crontab(task.cron_expression, timezone=tz)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    after = after or datetime.now(UTC)
    previous = _as_aware(task.last_run_at) if task.last_run_at else after.astimezone(tz)
    return trigger.get_next_fire_time(previous, after.astimezone(tz))


def validate_schedule(cron_expression: str, timezone: str) -> None:
    try:
        tz = ZoneInfo(timezone)
        CronTrigger.from_crontab(cron_expression, timezone=tz)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("cron 表达式或时区无效") from exc


def build_cron_expression(
    schedule_type: str,
    scheduled_time: str,
    weekdays: str = "mon-fri",
    custom_cron: str = "",
) -> str:
    if schedule_type == "custom":
        expression = custom_cron.strip()
    else:
        try:
            hour, minute = (int(value) for value in scheduled_time.split(":", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("执行时间必须是 HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("执行时间必须是有效的 HH:MM")
        day_of_week = "*" if schedule_type == "daily" else weekdays.strip() or "mon-fri"
        expression = f"{minute} {hour} * * {day_of_week}"
    validate_schedule(expression, "UTC")
    return expression


def run_task_now(
    session,
    task: Task,
    settings: Settings,
    run_key: str,
    now: datetime,
) -> JobRun:
    """Run the full pipeline for a task: sync -> filter -> summarize -> deliver.

    Creates or reuses the JobRun identified by run_key and returns it. A
    successful run stores the report (web channel) and, when email targets are
    configured, delivers it over SMTP; a failed delivery marks the job failed
    while the stored report remains retryable from the report page.
    """
    user = session.get(User, task.user_id)
    mailbox = session.get(Mailbox, task.mailbox_id)
    if not user or not mailbox:
        raise RuntimeError("任务关联的用户或邮箱不存在")
    job = session.scalar(select(JobRun).where(JobRun.run_key == run_key))
    if job and job.status == "success":
        task.last_run_at = now
        session.commit()
        return job
    if job is None:
        job = JobRun(
            user_id=user.id,
            mailbox_id=mailbox.id,
            task_id=task.id,
            run_key=run_key,
            stage="sync",
            status="running",
        )
        session.add(job)
        session.flush()
    else:
        job.stage = "sync"
        job.status = "running"
        job.error_message = None
        job.finished_at = None

    stage = "sync"
    try:
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
        MailSyncService(session, settings).sync(mailbox, connector)
        stage = job.stage = "summarize"
        period_start = (
            _as_aware(task.last_run_at)
            if task.last_run_at
            else now - timedelta(hours=max(task.lookback_hours, 1))
        )
        report = ReportService(session, settings).generate_for_user(
            user,
            task=task,
            period_start=period_start,
            period_end=now,
            run_key=run_key,
        )
        # Persist the report before SMTP so a failed delivery can be retried without
        # repeating synchronization and model inference.
        session.commit()
        targets = [item for item in task.delivery_targets if item.is_enabled]
        if targets:
            if not mailbox.smtp_host:
                raise RuntimeError("任务配置了邮件投递目标，但邮箱尚未配置 SMTP 发信服务")
            stage = job.stage = "delivery"
            service = ReportDeliveryService(session, settings)
            _deliver_to_targets(session, service, report, task, mailbox)
        job = session.get(JobRun, job.id)
        task = session.get(Task, task.id)
        job.stage = "complete"
        job.status = "success"
        job.finished_at = datetime.now(UTC)
        task.last_run_at = now
        session.commit()
        return job
    except Exception as exc:
        session.rollback()
        job = session.get(JobRun, job.id) if job.id else None
        if job is None:
            job = JobRun(
                user_id=user.id,
                mailbox_id=mailbox.id,
                task_id=task.id,
                run_key=run_key,
            )
            session.add(job)
        job.stage = stage
        job.status = "failed"
        job.error_message = error_message(exc, f"任务在 {stage} 阶段失败")
        job.details = {"error_type": type(exc).__name__, "stage": stage}
        job.finished_at = datetime.now(UTC)
        session.commit()
        logger.error("task {} failed at {}: {}", task.id, stage, type(exc).__name__)
        return job


def _deliver_to_targets(session, service, report, task: Task, mailbox) -> None:
    """Deliver a report to every enabled target of the task.

    Each target gets its own Delivery record (reused for retries); raises
    RuntimeError when any target fails.
    """
    for target in task.delivery_targets:
        if not target.is_enabled:
            continue
        delivery = session.scalar(
            select(Delivery)
            .where(
                Delivery.report_id == report.id,
                Delivery.destination == target.destination,
            )
            .order_by(Delivery.created_at.desc())
        )
        if delivery and delivery.status == "sent":
            continue
        if delivery:
            delivery = service.retry_delivery(delivery, report, mailbox)
        else:
            delivery = service.send_report(report, mailbox, target.destination)
        if delivery.status != "sent":
            raise RuntimeError("SMTP 投递失败")


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
