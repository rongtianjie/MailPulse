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
from .models import Delivery, JobRun, Mailbox, Schedule, User
from .report_service import ReportService
from .security import decrypt_secret


def run_due_schedules(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    session = build_session_factory(settings)()
    completed = 0
    try:
        now = datetime.now(UTC)
        schedules = list(session.scalars(select(Schedule).where(Schedule.is_enabled.is_(True))))
        for schedule in schedules:
            scheduled_fire = _due_fire_time(schedule, now)
            if scheduled_fire is None:
                continue
            try:
                completed += int(_run_schedule(session, schedule, now, scheduled_fire, settings))
            except Exception as exc:
                session.rollback()
                logger.error("schedule {} failed: {}", schedule.id, type(exc).__name__)
        return completed
    finally:
        session.close()


def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_due_schedules,
        "interval",
        seconds=30,
        kwargs={"settings": settings},
        id="mailpulse-schedule-poll",
        max_instances=1,
        coalesce=True,
    )
    run_due_schedules(settings)
    scheduler.start()


def _is_due(schedule: Schedule, now: datetime) -> bool:
    return _due_fire_time(schedule, now) is not None


def _due_fire_time(schedule: Schedule, now: datetime) -> datetime | None:
    try:
        tz = ZoneInfo(schedule.timezone)
    except ZoneInfoNotFoundError:
        return None
    local_now = now.astimezone(tz)
    try:
        trigger = CronTrigger.from_crontab(schedule.cron_expression, timezone=tz)
    except (TypeError, ValueError):
        return None
    if schedule.last_run_at is None:
        # A newly created schedule can run only its current day's reached slot.
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
    previous = _as_aware(schedule.last_run_at).astimezone(tz)
    next_fire = trigger.get_next_fire_time(previous, local_now)
    return next_fire if next_fire is not None and next_fire <= local_now else None


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


def _run_schedule(
    session, schedule: Schedule, now: datetime, scheduled_fire: datetime, settings: Settings
) -> bool:
    user = session.get(User, schedule.user_id)
    mailbox = session.get(Mailbox, schedule.mailbox_id)
    if not user or not mailbox:
        raise RuntimeError("任务关联的用户或邮箱不存在")
    run_key = f"schedule:{schedule.id}:{scheduled_fire.astimezone(UTC).isoformat()}"
    job = session.scalar(select(JobRun).where(JobRun.run_key == run_key))
    if job and job.status == "success":
        schedule.last_run_at = now
        session.commit()
        return False
    if job is None:
        job = JobRun(
            user_id=user.id,
            mailbox_id=mailbox.id,
            schedule_id=schedule.id,
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
            _as_aware(schedule.last_run_at)
            if schedule.last_run_at
            else now - timedelta(hours=max(schedule.lookback_hours, 1))
        )
        report = ReportService(session, settings).generate_for_user(
            user,
            mailbox_id=mailbox.id,
            rule_set_id=schedule.rule_set_id,
            period_start=period_start,
            period_end=now,
            schedule_id=schedule.id,
            run_key=run_key,
        )
        # Persist the report before SMTP so a failed delivery can be retried without
        # repeating synchronization and model inference.
        session.commit()
        if mailbox.smtp_host:
            stage = job.stage = "delivery"
            delivery = session.scalar(
                select(Delivery)
                .where(Delivery.report_id == report.id)
                .order_by(Delivery.created_at.desc())
            )
            service = ReportDeliveryService(session, settings)
            if delivery and delivery.status == "sent":
                pass
            elif delivery:
                delivery = service.retry_delivery(delivery, report, mailbox)
            else:
                if not user.email:
                    session.commit()
                    raise RuntimeError("用户未配置邮箱，无法自动投递报告，请在报告页面手动发送")
                delivery = service.send_report(report, mailbox, user.email)
            if delivery.status != "sent":
                session.commit()
                raise RuntimeError("SMTP 投递失败")
        job = session.get(JobRun, job.id)
        schedule = session.get(Schedule, schedule.id)
        job.stage = "complete"
        job.status = "success"
        job.finished_at = datetime.now(UTC)
        schedule.last_run_at = now
        session.commit()
        return True
    except Exception as exc:
        session.rollback()
        job = session.get(JobRun, job.id) if job.id else None
        if job is None:
            job = JobRun(
                user_id=user.id,
                mailbox_id=mailbox.id,
                schedule_id=schedule.id,
                run_key=run_key,
            )
            session.add(job)
        job.stage = stage
        job.status = "failed"
        job.error_message = error_message(exc, f"任务在 {stage} 阶段失败")
        job.details = {"error_type": type(exc).__name__, "stage": stage}
        job.finished_at = datetime.now(UTC)
        session.commit()
        logger.error("schedule {} failed at {}: {}", schedule.id, stage, type(exc).__name__)
        return False


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
