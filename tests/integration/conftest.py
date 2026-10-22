"""Integration tests need the PostgreSQL from docker-compose.yml (in the dev container: host `db`).

They run against a separate database `<POSTGRES_DB>_test`, created fresh per test session, so
development data is never touched.
"""
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, text

from app.config import Settings
from app.db.session import create_db_engine
from tests.integration.alembic_helpers import downgrade


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    settings = Settings.from_env()
    return replace(settings, postgres_db=f"{settings.postgres_db}_test")


@pytest.fixture(scope="session")
def test_database(test_settings):
    admin = create_engine(test_settings.database_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = admin.dialect.identifier_preparer.quote(test_settings.postgres_db)
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {name}"))
    yield
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture
def engine(test_settings, test_database):
    engine = create_db_engine(test_settings)
    yield engine
    # Every test starts from an empty schema.
    downgrade(engine, "base")
    engine.dispose()
