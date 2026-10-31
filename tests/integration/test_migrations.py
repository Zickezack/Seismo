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


def test_full_texts_refused_before_are_blocked_and_failed_again_on_downgrade(engine):
    upgrade(engine, "d29ed1e9b63f")
    with engine.begin() as connection:
        for key, status, error in [("a", "failed", "HTTP 403"), ("b", "failed", "HTTP 401"), ("c", "failed", "HTTP 404"), ("d", "stored", None)]:
            connection.execute(
                text(
                    "INSERT INTO article (source_name, source_url, item_key, url, full_text_status, full_text_error) "
                    "VALUES ('BFS', 'https://example.org', :key, :url, :status, :error)"
                ),
                {"key": key, "url": f"https://example.org/{key}", "status": status, "error": error},
            )

    upgrade(engine)

    def statuses():
        with engine.connect() as connection:
            return dict(connection.execute(text("SELECT item_key, full_text_status FROM article")).all())

    assert statuses() == {"a": "blocked", "b": "blocked", "c": "failed", "d": "stored"}
    downgrade(engine, "d29ed1e9b63f")
    assert statuses() == {"a": "failed", "b": "failed", "c": "failed", "d": "stored"}


def test_tdm_columns_start_empty_and_reserved_full_texts_fail_on_downgrade(engine):
    upgrade(engine, "b573f2c351f0")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO fetch_host (host) VALUES ('example.org')"))
        connection.execute(text("INSERT INTO platform_settings (id, llm_max_parallel_calls) VALUES (1, 2)"))

    upgrade(engine)

    with engine.begin() as connection:
        host = connection.execute(text("SELECT tdmrep_status, tdmrep_rules, tdmrep_fetched_at FROM fetch_host")).one()
        assert tuple(host) == (None, None, None)
        assert connection.execute(text("SELECT tdm_blocked_domains FROM platform_settings")).scalar_one() is None
        connection.execute(
            text(
                "INSERT INTO article (source_name, source_url, item_key, url, full_text_status) "
                "VALUES ('BBC', 'https://example.org', 'a', 'https://example.org/a', 'reserved')"
            )
        )
    downgrade(engine, "b573f2c351f0")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT full_text_status FROM article")).scalar_one() == "failed"


def test_existing_sources_start_without_a_region(engine):
    upgrade(engine, "3152564cbc2b")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO source (name, adapter_type, url) VALUES ('EZB', 'rss', 'https://example.org')"))

    upgrade(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT region FROM source")).scalar_one() is None


def test_articles_stored_so_far_wait_for_normalization_and_know_where_their_time_came_from(engine):
    upgrade(engine, "d2bbb146df5d")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO source (name, adapter_type, url) VALUES ('EZB', 'rss', 'https://ecb.example/rss')"))
        connection.execute(text("INSERT INTO source (name, adapter_type, url, query) VALUES ('GDELT', 'gdelt', 'https://api.example', 'x')"))
        connection.execute(
            text(
                "INSERT INTO article (source_id, source_name, source_url, item_key, url, published_at) VALUES "
                "((SELECT id FROM source WHERE name = 'EZB'), 'EZB', 'https://ecb.example/rss', 'a', 'https://e.example/a', now()), "
                "((SELECT id FROM source WHERE name = 'GDELT'), 'GDELT', 'https://api.example', 'b', 'https://g.example/b', now()), "
                "(NULL, 'Weg', 'https://gone.example/rss', 'c', 'https://gone.example/c', now()), "
                "(NULL, 'Weg', 'https://gone.example/rss', 'd', 'https://gone.example/d', NULL)"
            )
        )

    upgrade(engine)

    with engine.connect() as connection:
        rows = dict(connection.execute(text("SELECT item_key, published_at_source FROM article")).all())
        statuses = set(connection.execute(text("SELECT normalization_status FROM article")).scalars())
    assert rows == {"a": "feed", "b": "gdelt", "c": "feed", "d": None}
    assert statuses == {"pending"}


def test_normalized_articles_are_normalized_again_to_recognise_their_duplicates(engine):
    upgrade(engine, "eba26a55a0f5")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO article (source_name, source_url, item_key, url, canonical_url, normalization_status, normalized_at) VALUES "
                "('Q', 'https://q.example/rss', 'done', 'https://q.example/a', 'https://q.example/a', 'done', now()), "
                "('Q', 'https://q.example/rss', 'failed', 'https://q.example/b', 'https://q.example/b', 'failed', now()), "
                "('Q', 'https://q.example/rss', 'waiting', 'https://q.example/c', NULL, 'pending', NULL)"
            )
        )

    upgrade(engine)

    query = "SELECT item_key, normalization_status, normalized_at IS NULL, content_hash, duplicate_of_id, duplicate_reason FROM article"
    with engine.connect() as connection:
        rows = {row[0]: row[1:] for row in connection.execute(text(query))}
    assert rows == {
        "done": ("pending", True, None, None, None),
        "failed": ("failed", False, None, None, None),
        "waiting": ("pending", True, None, None, None),
    }

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE article SET duplicate_of_id = (SELECT id FROM article WHERE item_key = 'done'), duplicate_reason = 'url' WHERE item_key = 'waiting'")
        )
    downgrade(engine, "eba26a55a0f5")
    upgrade(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM article WHERE duplicate_of_id IS NULL")).scalar_one() == 3


def test_articles_get_signatures_and_links_by_being_normalized_again(engine):
    upgrade(engine, "83294e0053ee")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO article (source_name, source_url, item_key, url, normalization_status, normalized_at) VALUES "
                "('Q', 'https://q.example/rss', 'done', 'https://q.example/a', 'done', now()), "
                "('Q', 'https://q.example/rss', 'failed', 'https://q.example/b', 'failed', now())"
            )
        )

    upgrade(engine)

    query = "SELECT item_key, normalization_status, text_signature, text_bands FROM article"
    with engine.connect() as connection:
        rows = {row[0]: row[1:] for row in connection.execute(text(query))}
        links = connection.execute(text("SELECT count(*) FROM article_link")).scalar_one()
    assert rows == {"done": ("pending", None, None), "failed": ("failed", None, None)}
    assert links == 0

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO article_link (article_id, other_id, similarity) "
                "SELECT min(id), max(id), 0.8 FROM article"
            )
        )
    downgrade(engine, "83294e0053ee")
    upgrade(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM article")).scalar_one() == 2


def test_existing_sources_extract_events_and_the_settings_row_gets_the_extraction_defaults(engine):
    upgrade(engine, "c609763963d4")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO source (name, adapter_type, url) VALUES ('EZB', 'rss', 'https://ecb.example/rss')"))
        connection.execute(text("INSERT INTO platform_settings (id) VALUES (1) ON CONFLICT DO NOTHING"))

    upgrade(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT extract_events FROM source")).scalar_one() is True
        settings = connection.execute(
            text("SELECT extraction_languages, extraction_min_text_chars, extraction_max_age_days, extractions_per_hour FROM platform_settings")
        ).one()
    assert tuple(settings) == ("de,en,es,fr,it,nl,pt", 500, 3, 30)

    downgrade(engine, "c609763963d4")
    upgrade(engine)


def test_extractions_stored_so_far_were_valid_and_an_invalid_run_goes_on_downgrade(engine):
    upgrade(engine, "0129b1c02d83")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO article (source_name, source_url, item_key, url) VALUES ('Q', 'https://q.example/rss', 'a', 'https://q.example/a')"))
        connection.execute(
            text(
                "INSERT INTO extraction (article_id, model, schema_version, duration_ms, input_chars, truncated) "
                "SELECT id, 'qwen3.5:9b', '1', 1000, 500, false FROM article"
            )
        )

    upgrade(engine)

    with engine.connect() as connection:
        row = connection.execute(text("SELECT validation_result, repaired, validation_errors FROM extraction")).one()
    assert tuple(row) == ("valid", False, None)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO extraction (article_id, model, schema_version, duration_ms, input_chars, truncated, validation_result) "
                "SELECT id, 'qwen3.5:9b', '2', 1000, 500, false, 'invalid' FROM article"
            )
        )
    downgrade(engine, "0129b1c02d83")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT schema_version FROM extraction")).scalars().all() == ["1"]
    upgrade(engine)


def test_candidates_stored_so_far_wait_for_their_event(engine):
    upgrade(engine, "29544a69c420")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO article (source_name, source_url, item_key, url) VALUES ('Q', 'https://q.example/rss', 'a', 'https://q.example/a')"))
        connection.execute(text("INSERT INTO extraction (article_id, model, schema_version, duration_ms, input_chars, truncated) SELECT id, 'm', '2', 1, 1, false FROM article"))
        connection.execute(text(
            "INSERT INTO event_candidate (extraction_id, article_id, position, event_type, status, title, summary) "
            "SELECT x.id, x.article_id, 0, 'accident', 'start', 'T', 'S' FROM extraction x"
        ))

    upgrade(engine)

    with engine.connect() as connection:
        row = connection.execute(text("SELECT event_id, assignment_method, assigned_at FROM event_candidate")).one()
        events = connection.execute(text("SELECT count(*) FROM event")).scalar_one()
    assert (tuple(row), events) == ((None, None, None), 0)
    downgrade(engine, "29544a69c420")
    upgrade(engine)


def test_the_entities_go_with_a_downgrade_and_the_reports_stay(engine):
    upgrade(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO article (source_name, source_url, item_key, url) VALUES ('EZB', 'https://e.org', 'a', 'https://e.org/a')"))
        connection.execute(text(
            "INSERT INTO extraction (article_id, model, schema_version, duration_ms, input_chars, truncated) "
            "SELECT id, 'm', '2', 1, 1, false FROM article"
        ))
        connection.execute(text(
            "INSERT INTO event_candidate (extraction_id, article_id, position, event_type, status, title, summary) "
            "SELECT id, article_id, 0, 'other', 'update', 'Report', 'Summary.' FROM extraction"
        ))
        connection.execute(text("INSERT INTO entity (kind, name) VALUES ('person', 'Volodymyr Zelenskyy')"))
        connection.execute(text(
            "INSERT INTO entity_alias (entity_id, scheme, value, key, match) SELECT id, 'name', name, lower(name), 'founding' FROM entity"
        ))
        connection.execute(text(
            "INSERT INTO entity_mention (candidate_id, alias_id, source, position) "
            "SELECT c.id, a.id, 'actor', 0 FROM event_candidate c, entity_alias a"
        ))
        connection.execute(text("INSERT INTO entity_recognition (extraction_id, schema_version) SELECT id, '1' FROM extraction"))

    downgrade(engine, "0a882e94c29f")

    with engine.connect() as connection:
        tables = set(connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
        reports = connection.execute(text("SELECT count(*) FROM event_candidate")).scalar_one()
        trigram = connection.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")).scalar_one()
    assert not tables & {"entity", "entity_alias", "entity_mention", "entity_recognition"}
    assert (reports, trigram) == (1, 0)

    upgrade(engine)
    assert revision_of(engine) == head_revision()


def test_the_relationships_go_with_a_downgrade_and_the_entities_stay(engine):
    upgrade(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO entity (kind, name) VALUES ('company', 'TSMC'), ('company', 'Apple')"))
        connection.execute(text(
            "INSERT INTO entity_relationship (kind, subject_id, object_id) "
            "SELECT 'supplies', min(id), max(id) FROM entity"
        ))
        connection.execute(text(
            "INSERT INTO relationship_record (relationship_id, origin, valid_from, review, note) "
            "SELECT id, 'analyst', '2024-01-01', 'confirmed', 'Notiz' FROM entity_relationship"
        ))

    downgrade(engine, "6544d7bab59f")

    with engine.connect() as connection:
        tables = set(connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
        entities = connection.execute(text("SELECT count(*) FROM entity")).scalar_one()
    assert not tables & {"entity_relationship", "relationship_record"} and entities == 2

    upgrade(engine)
    assert revision_of(engine) == head_revision()


def test_a_downgrade_keeps_the_new_readings_and_drops_the_replaced_ones_with_their_corrections(engine):
    upgrade(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO article (source_name, source_url, item_key, url) VALUES ('EZB', 'https://e.org', 'a', 'https://e.org/a')"))
        old, new = connection.execute(text(
            "INSERT INTO extraction (article_id, model, schema_version, duration_ms, input_chars, truncated) "
            "SELECT id, 'm', version, 1, 1, false FROM article, (VALUES ('1'), ('2')) AS versions(version) RETURNING id"
        )).scalars().all()
        connection.execute(text("UPDATE extraction SET replaced_by_id = :new WHERE id = :old"), {"new": new, "old": old})
        old_report, new_report = connection.execute(text(
            "INSERT INTO event_candidate (extraction_id, article_id, position, event_type, status, title, summary) "
            "SELECT id, article_id, 0, 'other', 'update', 'Report', 'Summary.' FROM extraction ORDER BY id RETURNING id"
        )).scalars().all()
        event_id = connection.execute(text(
            "INSERT INTO event (title, event_type, first_reported_at, last_reported_at, founding_candidate_id) "
            "VALUES ('Report', 'other', now(), now(), :report) RETURNING id"
        ), {"report": new_report}).scalar_one()
        connection.execute(text("UPDATE event_candidate SET event_id = :event WHERE id = :report"), {"event": event_id, "report": new_report})
        correction_id = connection.execute(text(
            "INSERT INTO event_correction (kind, event_id, moves) VALUES ('reprocess', :event, '[]') RETURNING id"
        ), {"event": event_id}).scalar_one()
        connection.execute(text(
            "INSERT INTO event_state (event_id, status, valid_from, candidate_id, withdrawn_by_id) VALUES "
            "(:event, 'update', now(), :old_report, :correction), (:event, 'update', now(), :new_report, NULL)"
        ), {"event": event_id, "old_report": old_report, "new_report": new_report, "correction": correction_id})
        connection.execute(text("INSERT INTO entity (kind, name) VALUES ('company', 'TSMC'), ('company', 'Apple')"))
        connection.execute(text("INSERT INTO entity_relationship (kind, subject_id, object_id) SELECT 'supplies', min(id), max(id) FROM entity"))
        connection.execute(text(
            "INSERT INTO relationship_record (relationship_id, origin, candidate_id, reported_on, replaced_at) "
            "SELECT r.id, 'report', c.id, '2026-10-31', CASE WHEN c.id = :old_report THEN now() END "
            "FROM entity_relationship r, event_candidate c"
        ), {"old_report": old_report})
        connection.execute(text("INSERT INTO reprocessing (step, schema_version, model, total) VALUES ('extraction', '1', 'm', 1)"))
        connection.execute(text(
            "INSERT INTO storage_measurement (measured_on, database_bytes, table_bytes, article_count, raw_text_count, "
            "raw_text_bytes, backup_count, backup_bytes) VALUES ('2026-10-31', 1, '{}', 1, 0, 0, 0, 0)"
        ))

    downgrade(engine, "2182ff71ab31")

    with engine.connect() as connection:
        tables = set(connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
        extractions = connection.execute(text("SELECT id FROM extraction")).scalars().all()
        states = connection.execute(text("SELECT candidate_id FROM event_state")).scalars().all()
        corrections = connection.execute(text("SELECT count(*) FROM event_correction")).scalar_one()
        records = connection.execute(text("SELECT candidate_id FROM relationship_record")).scalars().all()
    assert not tables & {"reprocessing", "storage_measurement"}
    # As if the article had only ever been read the new way.
    assert (extractions, states, corrections, records) == ([new], [new_report], 0, [new_report])

    upgrade(engine)
    assert revision_of(engine) == head_revision()


def test_full_texts_failed_on_an_oversized_lookup_are_fetched_again_behind_live_work(engine):
    upgrade(engine, "fb4c52ab16d6")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO article (source_name, source_url, item_key, url, full_text_status, full_text_error) VALUES "
            "('G', 'https://g.org', 'a', 'https://a.org/a', 'failed', 'Antwort grösser als 100 KB'), "
            "('G', 'https://g.org', 'b', 'https://b.org/b', 'failed', 'Antwort grösser als 500 KB'), "
            "('G', 'https://g.org', 'c', 'https://c.org/c', 'failed', 'HTTP 404'), "
            "('G', 'https://g.org', 'd', 'https://d.org/d', 'stored', NULL)"
        ))

    upgrade(engine)

    with engine.connect() as connection:
        articles = connection.execute(text("SELECT item_key, full_text_status, full_text_error FROM article ORDER BY item_key")).all()
        queued = connection.execute(text(
            "SELECT a.item_key, j.max_attempts, j.priority, j.dedupe_key = 'fetch_article:' || a.id FROM job j "
            "JOIN article a ON a.id = (j.payload->>'article_id')::bigint WHERE j.kind = 'fetch_article' ORDER BY a.item_key"
        )).all()
    assert [tuple(row) for row in articles] == [
        ("a", "pending", None), ("b", "pending", None), ("c", "failed", "HTTP 404"), ("d", "stored", None),
    ]
    assert [tuple(row) for row in queued] == [("a", 31, 1, True), ("b", 31, 1, True)]
