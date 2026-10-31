"""The raw text lifecycle in PostgreSQL: a raw text deleted once its source's retention period is over since its
article's processing ended - and only then -, the article keeping what was read from it (raw_text_service)."""
import copy
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update

from app.db.models import Article, Evidence, Extraction, RawText, Source
from app.repositories import extraction_repository
from app.services import article_service, raw_text_service, reprocessing_service, source_service
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_extraction import add, source
from tests.integration.test_ingestion import NOW
from tests.integration.test_normalization import normalize_all
from tests.integration.test_reprocessing import OLD_REPORT

DAY = timedelta(days=1)


def article_with_text(database, key, source_id=None, published_at=NOW):
    article_id = add(database, source_id or source(database, f"S{key}"), key, published_at=published_at)
    normalize_all(database)
    return article_id


def extract(database, article_id, valid=True):
    """A run of the model on the article: valid with a report and its quote, or unusable."""
    values = {"article_id": article_id, "model": "m", "schema_version": "2", "duration_ms": 1, "input_chars": 1, "truncated": False}
    with database.begin() as connection:
        if valid:
            return extraction_repository.insert_extraction(connection, values, [copy.deepcopy(OLD_REPORT)])
        return extraction_repository.insert_run(connection, values | {"validation_result": "incomplete"})


def back(database, article_id, days):
    """As if the article's normalization and extraction had been `days` ago."""
    with database.begin() as connection:
        connection.execute(update(Article).where(Article.id == article_id).values(normalized_at=Article.normalized_at - days * DAY))
        connection.execute(update(Extraction).where(Extraction.article_id == article_id).values(extracted_at=Extraction.extracted_at - days * DAY))


def kept(database):
    with database.connect() as connection:
        return set(connection.execute(select(RawText.article_id)).scalars())


def article(database, article_id):
    with database.connect() as connection:
        return connection.execute(select(Article.__table__).where(Article.id == article_id)).one()


def test_a_raw_text_goes_once_its_retention_period_is_over_since_its_extraction_and_the_article_keeps_the_rest(database):
    within = article_with_text(database, "a")
    over = article_with_text(database, "b")
    for article_id, days in ((within, 29), (over, 31)):
        extract(database, article_id)
        back(database, article_id, days)
    before = article(database, over)

    assert raw_text_service.delete_due(database) == 1

    assert kept(database) == {within}
    after = article(database, over)
    assert after.raw_text_deleted_at is not None
    # What was read from it stays: link, language, hash and fingerprint - and the quotes of its reports.
    assert (after.canonical_url, after.detected_language, after.content_hash, after.text_signature) == (
        before.canonical_url, before.detected_language, before.content_hash, before.text_signature,
    )
    with database.connect() as connection:
        assert connection.execute(select(Evidence.quote).where(Evidence.article_id == over)).scalars().all()
    assert article_service.detail(database, over).article.text_note == "Rohtext gelöscht"
    assert raw_text_service.delete_due(database) == 0


def test_with_no_retention_a_raw_text_goes_right_after_its_processing(database):
    source_id = source(database, "Sofort")
    source_service.update(database, source_id, {**source_form(database, source_id), "raw_text_retention_days": "0"})
    extracted = article_with_text(database, "a", source_id)
    extract(database, extracted)
    waiting = article_with_text(database, "b", source_id)

    raw_text_service.delete_due(database)

    # The other one may still be extracted: it keeps its text.
    assert kept(database) == {waiting}


def source_form(database, source_id):
    with database.connect() as connection:
        row = connection.execute(select(Source.__table__).where(Source.id == source_id)).one()
    return {"name": row.name, "adapter_type": row.adapter_type, "url": row.url, "is_active": "on", "extract_events": "on",
            "fetch_interval_minutes": str(row.fetch_interval_minutes), "raw_text_retention_days": str(row.raw_text_retention_days),
            "region": row.region}


def test_an_article_never_extracted_counts_as_processed_once_the_extraction_can_take_it_no_more(database):
    # Extraction's maximum age: 3 days (Plattform-Einstellungen). Within it, and just out of it.
    young = article_with_text(database, "a", published_at=NOW)
    aged_out = article_with_text(database, "b", published_at=NOW - 4 * DAY)
    long_ago = article_with_text(database, "c", published_at=NOW - 40 * DAY)
    back(database, long_ago, 31)
    # Published as long ago, but normalized only now - found by a backfill: its processing ended now.
    found_late = article_with_text(database, "d", published_at=NOW - 40 * DAY)

    raw_text_service.delete_due(database)

    # The retention period counts from the end of its processing - the extraction's window over, or its normalization
    # when later: only the one long out of both goes.
    assert kept(database) == {young, aged_out, found_late}
    assert long_ago not in kept(database)


def test_a_duplicate_counts_from_its_normalization(database):
    original = article_with_text(database, "a")
    with database.begin() as connection:
        # A copy of it on another host, stored and normalized like it: a duplicate by its text.
        connection.execute(text("UPDATE article SET published_at = published_at - interval '1 hour' WHERE id = :id"), {"id": original})
    copy_id = add(database, source(database, "Kopie"), "b", host="copy.example")
    normalize_all(database)
    assert article(database, copy_id).duplicate_of_id == original
    extract(database, original)
    back(database, copy_id, 31)

    raw_text_service.delete_due(database)

    assert kept(database) == {original}


def test_a_failed_processing_keeps_its_raw_text_until_a_retry_succeeds(database):
    unreadable = article_with_text(database, "a", published_at=NOW - 40 * DAY)
    with database.begin() as connection:
        connection.execute(update(Article).where(Article.id == unreadable).values(normalization_status="failed"))
    unusable = article_with_text(database, "b", published_at=NOW - 40 * DAY)
    extract(database, unusable, valid=False)

    raw_text_service.delete_due(database)

    assert kept(database) == {unreadable, unusable}
    lifecycle = raw_text_service.lifecycle(database)
    assert (lifecycle.failed, lifecycle.processing, lifecycle.kept, lifecycle.due, lifecycle.deleted) == (2, 0, 0, 0, 0)
    # The model asked again, successfully: from then on the rule applies.
    extract(database, unusable)
    back(database, unusable, 31)
    raw_text_service.delete_due(database)
    assert kept(database) == {unreadable}


def test_an_article_of_a_deleted_source_keeps_its_raw_text_for_the_default_period(database):
    source_id = source(database, "Weg")
    within = article_with_text(database, "a", source_id)
    over = article_with_text(database, "b", source_id)
    for article_id, days in ((within, 29), (over, 31)):
        extract(database, article_id)
        back(database, article_id, days)
    source_service.delete(database, [source_id])

    raw_text_service.delete_due(database)

    assert kept(database) == {within}


def test_the_lifecycle_counts_each_raw_text_once(database):
    article_with_text(database, "a")
    processed = article_with_text(database, "b")
    extract(database, processed)
    due = article_with_text(database, "c")
    extract(database, due)
    back(database, due, 31)

    before = raw_text_service.lifecycle(database)
    raw_text_service.delete_due(database)
    after = raw_text_service.lifecycle(database)

    assert (before.processing, before.kept, before.due, before.failed, before.deleted) == (1, 1, 1, 0, 0)
    assert (after.processing, after.kept, after.due, after.failed, after.deleted) == (1, 1, 0, 0, 1)


def test_an_article_normalized_again_without_its_raw_text_keeps_what_was_read_from_it(database):
    original = article_with_text(database, "a")
    copy_id = add(database, source(database, "Kopie"), "b", host="copy.example")
    normalize_all(database)
    assert article(database, copy_id).duplicate_of_id == original
    back(database, copy_id, 31)
    raw_text_service.delete_due(database)
    before = article(database, copy_id)
    assert before.raw_text_deleted_at is not None

    # Kein Duplikat: an original of its own, normalized again - without its raw text.
    assert article_service.not_duplicate(database, [copy_id]).processed == 1
    normalize_all(database)

    after = article(database, copy_id)
    assert (after.normalization_status, after.duplicate_of_id) == ("done", None)
    assert (after.content_hash, after.text_signature, after.detected_language, after.canonical_url) == (
        before.content_hash, before.text_signature, before.detected_language, before.canonical_url,
    )


def test_an_article_whose_raw_text_is_gone_cannot_be_extracted_again(database):
    over = article_with_text(database, "a")
    values = {"article_id": over, "model": "qwen3.5:9b", "schema_version": "1", "duration_ms": 1, "input_chars": 1, "truncated": False}
    with database.begin() as connection:
        extraction_repository.insert_extraction(connection, values, [copy.deepcopy(OLD_REPORT)])
    back(database, over, 31)

    raw_text_service.delete_due(database)

    preview = reprocessing_service.overview(database, "qwen3.5:9b", {"group": "extraction|1|qwen3.5:9b"}).preview
    assert (preview.count, preview.without_text) == (0, 1)
    with pytest.raises(reprocessing_service.InvalidReprocessing, match="nichts, was neu verarbeitet werden kann"):
        reprocessing_service.start(database, {"group": "extraction|1|qwen3.5:9b"}, "qwen3.5:9b")
