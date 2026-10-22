from alembic import command
from sqlalchemy import Engine

from app.db.migrations import alembic_config


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as connection:
        command.upgrade(_config_for(connection), revision)


def downgrade(engine: Engine, revision: str = "base") -> None:
    with engine.begin() as connection:
        command.downgrade(_config_for(connection), revision)


def _config_for(connection):
    # migrations/env.py uses this connection instead of the database from the environment.
    config = alembic_config()
    config.attributes["connection"] = connection
    return config
