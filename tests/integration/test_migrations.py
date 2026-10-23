from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

import app.db.models  # noqa: F401 - registers the tables on Base.metadata
from app.db.base import Base
from app.db.migrations import head_revision
from app.repositories import (
    article_repository,
    fetch_host_repository,
    platform_settings_repository,
    raw_text_repository,
    seed_log_repository,
    source_repository,
)
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
        [article_id] = article_repository.insert_new(connection, [{
            "source_id": source_id, "source_name": "EZB", "source_url": "https://example.org", "item_key": "a",
            "url": "https://example.org/a", "title": None, "published_at": None, "language": None,
        }])
        raw_text_repository.insert_raw_text(connection, article_id, b"<p>a</p>", "text/html", "https://example.org/a")
        fetch_host_repository.save_robots(connection, "example.org", "allow_all", None)

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


def test_articles_stored_before_full_texts_existed_are_skipped(engine):
    upgrade(engine, "2eb9974bce2d")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO article (source_name, source_url, item_key, url) "
                "VALUES ('EZB', 'https://example.org', 'a', 'https://example.org/a')"
            )
        )

    upgrade(engine)

    with engine.connect() as connection:
        status = connection.execute(text("SELECT full_text_status, full_text_error FROM article")).one()
    assert tuple(status) == ("skipped", "Abgerufen, bevor es Volltexte gab")


def test_existing_sources_and_hosts_start_without_failures_or_pause(engine):
    upgrade(engine, "80e736473827")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO source (name, adapter_type, url) VALUES ('EZB', 'rss', 'https://example.org')"))
        connection.execute(text("INSERT INTO fetch_host (host) VALUES ('example.org')"))

    upgrade(engine)

    with engine.connect() as connection:
        status = connection.execute(
            text("SELECT min_request_interval_seconds, last_error, last_error_at, consecutive_failures FROM source")
        ).one()
        paused_until = connection.execute(text("SELECT paused_until FROM fetch_host")).scalar_one()
    assert tuple(status) == (None, None, None, 0)
    assert paused_until is None


def test_existing_sources_have_no_query(engine):
    upgrade(engine, "856511d67c19")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO source (name, adapter_type, url) VALUES ('EZB', 'rss', 'https://example.org')"))

    upgrade(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT query FROM source")).scalar_one() is None


def test_jobs_queued_before_backfills_existed_are_live_work(engine):
    upgrade(engine, "e2a68a4ed2e0")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO job (kind) VALUES ('fetch_source')"))
        connection.execute(
            text(
                "INSERT INTO article (source_name, source_url, item_key, url) "
                "VALUES ('EZB', 'https://example.org', 'a', 'https://example.org/a')"
            )
        )

    upgrade(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT priority FROM job")).scalar_one() == 0
        assert connection.execute(text("SELECT backfill_id FROM article")).scalar_one() is None
