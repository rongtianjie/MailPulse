from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from mailpulse import models  # noqa: F401
from mailpulse.config import get_settings
from mailpulse.db import Base
from mailpulse.logging_config import configure_logging

config = context.config
settings = config.attributes.get("mailpulse_settings") or get_settings()
if not config.attributes.get("logging_configured"):
    configure_logging(settings)
x_args = context.get_x_argument(as_dictionary=True)
database_url = x_args.get("db_url") or settings.resolved_database_url
config.set_main_option("sqlalchemy.url", database_url)
target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to):
    """Keep the optional FTS5 virtual table outside Alembic schema ownership."""
    if type_ == "table" and name.startswith("message_search"):
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
