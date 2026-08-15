from __future__ import annotations

import inspect
import logging
import sys
from logging import LogRecord

from loguru import logger

from .config import Settings


class InterceptHandler(logging.Handler):
    """Forward standard-library logs to Loguru."""

    def emit(self, record: LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = inspect.currentframe()
        depth = 0
        while frame is not None and (
            depth == 0 or frame.f_code.co_filename == logging.__file__
        ):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(settings: Settings) -> None:
    """Configure console and rolling file sinks for the application."""
    logger.remove()
    level = settings.log_level.upper()
    logger.add(
        sys.stdout,
        level=level,
        enqueue=False,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level:<8}</level> | {name}:{function}:{line} - {message}",
    )
    logger.add(
        settings.logs_dir / "mailpulse.log",
        level=level,
        rotation=settings.log_rotation,
        retention=settings.log_retention,
        compression="gz",
        encoding="utf-8",
        enqueue=False,
        filter=lambda record: not record["extra"].get("console_only", False),
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
        "{name}:{function}:{line} - {message}",
    )

    intercept = InterceptHandler()
    logging.basicConfig(handlers=[intercept], level=0, force=True)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "apscheduler"):
        standard_logger = logging.getLogger(name)
        standard_logger.handlers = [intercept]
        standard_logger.propagate = False
