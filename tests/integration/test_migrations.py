from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

import app.db.models  # noqa: F401 - registers the tables on Base.metadata
from app.db.base import Base
from app.db.migrations import head_revision
from app.repositories import platform_settings_repository, seed_log_repository, source_repository
from app.repositories.schema_repository import current_revision
from app.services import job_service, platform_settings_service
from tests.integration.alembic_helpers import downgrade, upgrade


def revision_of(engine):
    with engine.connect() as connection:
        return current_revision(connection)


def test_upgrade_downgrade_and_upgrade_again(engine):
    assert revision_of(engine) is None

    upgrade(engine)
    assert revision_of(engine) == head_revision()

    downgrade(engine)
    assert revision_of(engine) is None

    upgrade(engine)
    assert revision_of(engine) == head_revision()


def test_migrated_schema_matches_the_models(engine):
    upgrade(engine)

    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_downgrade_and_upgrade_with_rows_in_place(engine):
    upgrade(engine)
    with engine.begin() as connection:
        job_service.enqueue(connection, "testjob", dedupe_key="article:1")
        platform_settings_repository.save(connection, {"llm_max_parallel_calls": 2})
        seed_log_repository.record(connection, "sources", "ecb")
        source_repository.insert_source(connection, {"name": "EZB", "adapter_type": "rss", "url": "https://example.org"})

    downgrade(engine)
    upgrade(engine)

    assert revision_of(engine) == head_revision()


def test_backup_settings_get_their_defaults_in_an_existing_settings_row(engine):
    upgrade(engine, "724a65db6143")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO platform_settings (id, llm_max_parallel_calls) VALUES (1, 3)"))

    upgrade(engine)

    with engine.connect() as connection:
        settings = platform_settings_service.load(connection)
    assert (settings.llm_max_parallel_calls, settings.backup_interval_hours, settings.backups_to_keep) == (3, 24, 14)
