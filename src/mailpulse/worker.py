from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import select, update

from .config import Settings, get_settings
from .db import build_session_factory
from .delivery import ReportDeliveryService
from .errors import error_message
from .mail.connectors import IMAPConnector
from .mail.sync import MailSyncService
from .mail.types import MailboxConnection
from .models import Delivery, JobRun, Mailbox, Task, User, utc_now
from .report_service import ReportService
from .security import decrypt_secret

ACTIVE_JOB_STATUSES = ("queued", "running")


class JobCancelled(RuntimeError):
    """Raised when a worker observes a cooperative cancellation request."""


def _job_details(job: JobRun) -> dict:
    details = dict(job.details or {})
    details.setdefault("events", [])
    details.setdefault("summary", {})
    return details


def append_job_log(
    session,
    job: JobRun,
    message: str,
    *,
    stage: str | None = None,
    level: str = "info",
    data: dict | None = None,
    commit: bool = False,
) -> None:
    details = _job_details(job)
    event = {
        "at": utc_now().isoformat(),
        "stage": stage or job.stage,
        "level": level,
        "message": message,
    }
    if data:
        event["data"] = data
        details["summary"] = {**details.get("summary", {}), **data}
    events = [*details.get("events", []), event]
    details["events"] = events[-200:]
    job.details = details
    if commit:
        session.commit()


def _set_job_stage(session, job: JobRun, stage: str, message: str) -> None:
    job.stage = stage
    append_job_log(session, job, message, stage=stage, commit=True)


def _check_cancelled(session, job: JobRun) -> None:
    session.refresh(job)
    if job.cancel_requested:
        raise JobCancelled("用户取消了本次运行")


def enqueue_job_run(
    session,
    task: Task,
    user: User,
    *,
    run_kind: str = "task",
    run_key: str | None = None,
    scheduled_fire_at: datetime | None = None,
) -> JobRun:
    run_key = run_key or f"manual:{task.id}:{uuid4().hex}"
    existing = session.scalar(select(JobRun).where(JobRun.run_key == run_key))
    if existing is not None:
        return existing
    claimed = session.execute(
        update(Task)
        .where(Task.id == task.id, Task.active_run_key.is_(None))
        .values(active_run_key=run_key)
    ).rowcount
    if claimed != 1:
        raise ValueError("该任务已有运行中的任务")
    job = JobRun(
        user_id=user.id,
        mailbox_id=task.mailbox_id,
        task_id=task.id,
        run_key=run_key,
        run_kind=run_kind,
        stage="sync",
        status="queued",
        scheduled_fire_at=scheduled_fire_at,
        details={"events": [], "summary": {}},
    )
    session.add(job)
    session.flush()
    append_job_log(session, job, "已提交，等待后台 worker 执行", stage="queued")
    return job


def _claim_existing_job_task(session, task: Task, run_key: str) -> None:
    """Claim both task and mailbox so duplicate syncs cannot run concurrently."""
    task_claimed = session.execute(
        update(Task)
        .where(Task.id == task.id, Task.active_run_key.is_(None))
        .values(active_run_key=run_key)
    ).rowcount
    if task_claimed != 1:
        active = session.scalar(
            select(JobRun).where(
                JobRun.task_id == task.id,
                JobRun.run_key == run_key,
                JobRun.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active is None:
            raise ValueError("该任务已有运行中的任务")
    mailbox_claimed = session.execute(
        update(Mailbox)
        .where(Mailbox.id == task.mailbox_id, Mailbox.active_run_key.is_(None))
        .values(active_run_key=run_key)
    ).rowcount
    if mailbox_claimed != 1:
        active = session.scalar(
            select(JobRun).where(
                JobRun.mailbox_id == task.mailbox_id,
                JobRun.run_key == run_key,
                JobRun.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active is None:
            if task_claimed == 1:
                session.execute(
                    update(Task)
                    .where(Task.id == task.id, Task.active_run_key == run_key)
                    .values(active_run_key=None)
                )
            raise ValueError("该邮箱已有运行中的同步任务")


def _release_task_run(session, task_id: int, run_key: str) -> None:
    session.execute(
        update(Task)
        .where(Task.id == task_id, Task.active_run_key == run_key)
        .values(active_run_key=None)
    )
    task = session.get(Task, task_id)
    if task is not None:
        session.execute(
            update(Mailbox)
            .where(Mailbox.id == task.mailbox_id, Mailbox.active_run_key == run_key)
            .values(active_run_key=None)
        )


def _prune_job_logs(session, settings: Settings, now: datetime) -> None:
    """Keep run summaries while removing detailed events outside the retention window."""
    cutoff = now - timedelta(days=settings.job_log_retention_days)
    jobs = list(
        session.scalars(
            select(JobRun)
            .where(JobRun.finished_at.is_not(None))
            .order_by(JobRun.task_id.asc(), JobRun.finished_at.desc())
        )
    )
    ranks: dict[int | None, int] = {}
    changed = False
    for job in jobs:
        ranks[job.task_id] = ranks.get(job.task_id, 0) + 1
        if ranks[job.task_id] <= settings.job_log_retention_count:
            continue
        if job.finished_at is None or _as_aware(job.finished_at) >= cutoff:
            continue
        details = _job_details(job)
        if details.get("events"):
            details["events"] = []
            details["log_pruned"] = True
            job.details = details
            changed = True
    if changed:
        session.commit()


def _recover_stale_jobs(session, settings: Settings, now: datetime) -> None:
    """Release jobs left running after a worker process interruption."""
    cutoff = now - timedelta(hours=settings.job_stale_after_hours)
    stale_job_ids = list(
        session.scalars(
            select(JobRun.id).where(
                JobRun.status == "running", JobRun.started_at < cutoff
            )
        )
    )
    for job_id in stale_job_ids:
        job = session.get(JobRun, job_id)
        if job is None:
            continue
        error_message_text = "后台 worker 可能中断，已自动释放运行锁"
        details = _job_details(job)
        details.update({"error_type": "StaleJob", "failed_stage": job.stage})
        updated = session.execute(
            update(JobRun)
            .where(
                JobRun.id == job.id,
                JobRun.status == "running",
                JobRun.started_at < cutoff,
            )
            .values(
                status="failed",
                error_message=error_message_text,
                finished_at=now,
                details=details,
            )
        )
        if updated.rowcount != 1:
            session.expire(job)
            continue
        session.refresh(job)
        _release_task_run(session, job.task_id, job.run_key)
        append_job_log(
            session,
            job,
            error_message_text,
            stage=job.stage,
            level="error",
            commit=True,
        )


def run_due_tasks(settings: Settings | None = None) -> int:
    """Process queued jobs and enqueue/execute each reached Cron slot once."""
    settings = settings or get_settings()
    session = build_session_factory(settings)()
    completed = 0
    try:
        now = datetime.now(UTC)
        _recover_stale_jobs(session, settings, now)
        _prune_job_logs(session, settings, now)
        queued = list(
            session.scalars(
                select(JobRun)
                .where(JobRun.status == "queued")
                .order_by(JobRun.started_at.asc())
                .limit(settings.job_worker_batch_size)
            )
        )
        for job in queued:
            task = session.get(Task, job.task_id) if job.task_id else None
            if task is None:
                job.status = "failed"
                job.error_message = "任务不存在"
                job.finished_at = now
                session.commit()
                continue
            try:
                result = run_task_now(
                    session, task, settings, job.run_key, now, job=job, run_kind=job.run_kind
                )
                completed += int(result.status == "success")
            except Exception as exc:
                session.rollback()
                logger.error("queued job {} failed: {}", job.id, type(exc).__name__)
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
                if session.scalar(select(JobRun).where(JobRun.run_key == run_key)):
                    continue
                task.last_scheduled_fire_at = scheduled_fire.astimezone(UTC)
                job = enqueue_job_run(
                    session,
                    task,
                    session.get(User, task.user_id),
                    run_key=run_key,
                    scheduled_fire_at=scheduled_fire.astimezone(UTC),
                )
                session.commit()
                job = run_task_now(
                    session, task, settings, run_key, now, job=job, run_kind="task"
                )
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
    if task.last_run_at is None and task.last_scheduled_fire_at is None:
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
    previous_value = task.last_scheduled_fire_at or task.last_run_at
    previous = _as_aware(previous_value).astimezone(tz)
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
    previous_value = task.last_scheduled_fire_at or task.last_run_at
    previous = _as_aware(previous_value) if previous_value else after.astimezone(tz)
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
    *,
    job: JobRun | None = None,
    run_kind: str = "task",
) -> JobRun:
    """Run a queued task or sync job.

    Task jobs execute sync -> filter -> summarize -> deliver. Sync jobs stop
    after IMAP synchronization. The function remains directly callable by
    tests and CLI code, while web requests enqueue jobs for the worker.
    """
    user = session.get(User, task.user_id)
    mailbox = session.get(Mailbox, task.mailbox_id)
    if not user or not mailbox:
        raise RuntimeError("任务关联的用户或邮箱不存在")
    job = job or session.scalar(select(JobRun).where(JobRun.run_key == run_key))
    if job and job.status in {"success", "canceled"}:
        return job
    if job is None:
        _claim_existing_job_task(session, task, run_key)
        job = JobRun(
            user_id=user.id,
            mailbox_id=mailbox.id,
            task_id=task.id,
            run_key=run_key,
            stage="sync",
            status="running",
            run_kind=run_kind,
            details={"events": [], "summary": {}},
        )
        session.add(job)
        session.flush()
    else:
        _claim_existing_job_task(session, task, run_key)
        job.stage = "sync"
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.run_kind = run_kind
        job.cancel_requested = False
        job.error_message = None
        job.finished_at = None
    append_job_log(session, job, "worker 已开始执行", stage="sync", commit=True)

    stage = "sync"
    try:
        _check_cancelled(session, job)
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
        sync_result = MailSyncService(session, settings).sync(mailbox, connector)
        append_job_log(
            session,
            job,
            "IMAP 同步完成",
            stage="sync",
            data={
                "fetched": sync_result.fetched,
                "created": sync_result.created,
                "linked": sync_result.linked,
                "attachments": sync_result.attachments,
            },
            commit=True,
        )
        _check_cancelled(session, job)
        if run_kind == "sync":
            job.stage = "complete"
            job.status = "success"
            job.finished_at = datetime.now(UTC)
            _release_task_run(session, task.id, run_key)
            append_job_log(session, job, "仅同步任务完成", stage="complete", commit=True)
            return job
        _set_job_stage(session, job, "attachments", "开始处理附件和转换结果")
        stage = "attachments"
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
        _set_job_stage(session, job, "summarize", "开始使用主模型生成报告")
        stage = "summarize"
        report_summary = report.summary or {}
        append_job_log(
            session,
            job,
            "报告生成完成",
            stage="summarize",
            data={
                "matched_message_count": report_summary.get("matched_message_count", 0),
                "message_count": report_summary.get("message_count", 0),
                "truncated": report_summary.get("truncated", False),
            },
        )
        if report.model_trace.get("vision_error"):
            append_job_log(
                session,
                job,
                "视觉副模型失败，已降级使用主模型继续生成报告",
                stage="summarize",
                level="warning",
                data={"vision_error": report.model_trace.get("vision_error")},
            )
        # Persist the report before SMTP so a failed delivery can be retried without
        # repeating synchronization and model inference.
        session.commit()
        _check_cancelled(session, job)
        targets = [item for item in task.delivery_targets if item.is_enabled]
        if targets:
            if not mailbox.smtp_host:
                raise RuntimeError("任务配置了邮件投递目标，但邮箱尚未配置 SMTP 发信服务")
            _set_job_stage(session, job, "delivery", "开始 SMTP 投递")
            stage = "delivery"
            service = ReportDeliveryService(session, settings)
            _deliver_to_targets(session, service, report, task, mailbox, job=job)
        _check_cancelled(session, job)
        job = session.get(JobRun, job.id)
        task = session.get(Task, task.id)
        job.stage = "complete"
        job.status = "success"
        job.finished_at = datetime.now(UTC)
        task.last_run_at = now
        _release_task_run(session, task.id, run_key)
        append_job_log(session, job, "任务运行完成", stage="complete", commit=True)
        return job
    except JobCancelled as exc:
        session.rollback()
        job = session.get(JobRun, job.id) if job and job.id else None
        if job is None:
            raise
        job.status = "canceled"
        job.stage = stage
        job.error_message = None
        job.finished_at = datetime.now(UTC)
        _release_task_run(session, job.task_id, job.run_key)
        append_job_log(session, job, str(exc), stage=stage, level="warning", commit=True)
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
                run_kind=run_kind,
            )
            session.add(job)
        job.stage = stage
        job.status = "failed"
        job.error_message = error_message(exc, f"任务在 {stage} 阶段失败")
        details = _job_details(job)
        details.update({"error_type": type(exc).__name__, "failed_stage": stage})
        job.details = details
        job.finished_at = datetime.now(UTC)
        if stage == "sync":
            failed_mailbox = session.get(Mailbox, mailbox.id)
            if failed_mailbox is not None:
                failed_mailbox.sync_error = job.error_message
        _release_task_run(session, job.task_id, job.run_key)
        append_job_log(
            session,
            job,
            job.error_message,
            stage=stage,
            level="error",
            commit=True,
        )
        logger.error("task {} failed at {}: {}", task.id, stage, type(exc).__name__)
        return job


def _deliver_to_targets(
    session, service, report, task: Task, mailbox, *, job: JobRun | None = None
) -> None:
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
        if job is not None:
            append_job_log(
                session,
                job,
                f"投递 {target.destination}：{delivery.status}",
                stage="delivery",
                level="info" if delivery.status == "sent" else "error",
                data={"delivery_status": delivery.status, "destination": target.destination},
                commit=True,
            )
        if delivery.status != "sent":
            raise RuntimeError("SMTP 投递失败")


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
