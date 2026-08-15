from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def build_engine(settings: Settings | None = None):
    settings = settings or get_settings()
    connect_args = (
        {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
    )
    engine = create_engine(settings.resolved_database_url, connect_args=connect_args, future=True)
    if settings.resolved_database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def build_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=build_engine(settings), autoflush=False, expire_on_commit=False)


def init_database(settings: Settings | None = None) -> None:
    from .search import SearchService

    settings = settings or get_settings()
    _run_schema_migrations(settings)
    engine = build_engine(settings)
    index_state = SearchService.ensure_index(engine)
    if index_state in {"created", "rebuilt"}:
        # A fresh or rebuilt FTS index must be backfilled from existing messages.
        session = build_session_factory(settings)()
        try:
            SearchService(session).reindex_all()
        finally:
            session.close()


def _run_schema_migrations(settings: Settings) -> None:
    """Apply the checked-in Alembic schema before the app uses the database."""
    engine = build_engine(settings)
    table_names = set(inspect(engine).get_table_names())
    migration_config = _alembic_config(settings)
    if "alembic_version" not in table_names and _has_initial_schema(table_names):
        # Development builds created before Alembic was introduced used metadata.create_all.
        # The initial migration describes that same schema, so record its revision once.
        command.stamp(migration_config, "head")
    else:
        command.upgrade(migration_config, "head")


def _alembic_config(settings: Settings) -> AlembicConfig:
    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "alembic.ini"
    if not config_path.is_file():
        raise RuntimeError(f"找不到数据库迁移配置: {config_path}")
    config = AlembicConfig(str(config_path))
    config.set_main_option("sqlalchemy.url", settings.resolved_database_url)
    config.attributes["mailpulse_settings"] = settings
    return config


def _has_initial_schema(table_names: set[str]) -> bool:
    required = {
        "users",
        "mailboxes",
        "canonical_messages",
        "message_occurrences",
        "attachments",
        "rule_sets",
        "schedules",
        "ai_provider_profiles",
        "model_bindings",
        "reports",
        "deliveries",
        "job_runs",
        "audit_logs",
    }
    return required.issubset(table_names)


def session_scope(settings: Settings | None = None) -> Generator[Session, None, None]:
    factory = build_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
