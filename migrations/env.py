"""Alembic environment.

The URL comes from Pydantic Settings rather than alembic.ini, so the connection string
lives in exactly one place (.env) and never lands in the repo.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from funnel.config import get_settings
from funnel.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single source of truth for the URL: .env via Settings.
config.set_main_option("sqlalchemy.url", str(get_settings().database_url))

# What autogenerate diffs the database against.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without connecting to a database."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
