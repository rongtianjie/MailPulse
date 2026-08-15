from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings, get_settings


class Base(DeclarativeBase):
    pass


@dataclass(frozen=True)
class DefaultAdminCredentials:
    email: str
    password: str


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


def bootstrap_database(settings: Settings | None = None) -> DefaultAdminCredentials | None:
    """Initialize schema and create the default administrator when needed."""
    settings = settings or get_settings()
    init_database(settings)
    from .auth import create_user
    from .models import User

    session = build_session_factory(settings)()
    try:
        existing_admin = session.scalar(select(User).where(User.role == "admin").limit(1))
        if existing_admin is not None:
            return None
        existing_account = session.scalar(
            select(User).where(User.email == settings.default_admin_email.strip().lower())
        )
        if existing_account is not None:
            raise RuntimeError(f"默认管理员邮箱已被其他账号占用: {settings.default_admin_email}")
        create_user(
            session,
            settings.default_admin_email,
            settings.default_admin_password,
            settings.default_admin_display_name,
            role="admin",
            must_change_password=True,
        )
        session.commit()
        return DefaultAdminCredentials(
            email=settings.default_admin_email,
            password=settings.default_admin_password,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_database(settings: Settings | None = None) -> Path:
    """Remove the configured SQLite database and its sidecar files."""
    settings = settings or get_settings()
    database_url = make_url(settings.resolved_database_url)
    if database_url.get_backend_name() != "sqlite" or not database_url.database:
        raise RuntimeError("reset-db 目前只支持文件型 SQLite 数据库")
    database_path = Path(database_url.database).expanduser().resolve()
    if database_path.name == ":memory:":
        raise RuntimeError("reset-db 不支持内存数据库")
    for path in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        path.unlink(missing_ok=True)
    return database_path


def _run_schema_migrations(settings: Settings) -> None:
    """Apply the checked-in Alembic schema before the app uses the database."""
    engine = build_engine(settings)
    table_names = set(inspect(engine).get_table_names())
    migration_config = _alembic_config(settings)
    if "alembic_version" not in table_names and _has_initial_schema(table_names):
        # Development builds created before Alembic was introduced used metadata.create_all.
        # The initial migration describes that same schema, so record its revision once.
        user_columns = {column["name"] for column in inspect(engine).get_columns("users")}
        if "must_change_password" in user_columns:
            command.stamp(migration_config, "head")
        else:
            command.stamp(migration_config, "7ba9c0201269")
            command.upgrade(migration_config, "head")
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
    config.attributes["logging_configured"] = True
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
