from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

import app.db.models  # noqa: F401 - registers the tables on Base.metadata
from app.db.base import Base
from app.db.migrations import head_revision
from app.repositories import article_repository, platform_settings_repository, seed_log_repository, source_repository
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
        source_id = source_repository.insert_source(connection, {"name": "EZB", "adapter_type": "rss", "url": "https://example.org"})
        article_repository.insert_new(connection, [{
            "source_id": source_id, "source_name": "EZB", "source_url": "https://example.org", "item_key": "a",
            "url": "https://example.org/a", "title": None, "published_at": None, "language": None,
        }])

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


def test_existing_sources_are_due_and_the_settings_row_gets_the_maximum_article_age(engine):
    upgrade(engine, "5343ac035b27")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO platform_settings (id, llm_max_parallel_calls) VALUES (1, 3)"))
        connection.execute(text("INSERT INTO source (name, adapter_type, url) VALUES ('EZB', 'rss', 'https://example.org')"))

    upgrade(engine)

    with engine.connect() as connection:
        settings = platform_settings_service.load(connection)
        fetch_times = connection.execute(text("SELECT next_fetch_at, last_fetched_at FROM source")).one()
    assert (settings.llm_max_parallel_calls, settings.article_max_age_days) == (3, 7)
    assert tuple(fetch_times) == (None, None)
