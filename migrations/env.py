"""Alembic environment. The database URL comes only from environment variables (app/config.py)."""
from alembic import context
from sqlalchemy import create_engine, pool

from app.config import Settings
from app.db.base import Base
from app.logging_config import configure_logging

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=Settings.from_env().database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests pass their own connection, so migrations run against the test database.
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return

    settings = Settings.from_env()
    configure_logging("migrate", settings.log_level_number)
    engine = create_engine(settings.database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run_migrations(connection)


def _run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
