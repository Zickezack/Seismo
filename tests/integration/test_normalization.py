"""Normalization in PostgreSQL: which articles are due, what a job writes, the publication time's origin, articles
without a raw text and raw texts no reader understands."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from app.db.models import BACKFILL_PRIORITY, Article, FullTextStatus, Job, JobStatus, RawText
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.repositories import article_repository, raw_text_repository
from app.services import article_service, job_service, normalization_service, source_service
from tests.integration.alembic_helpers import upgrade
from tests.unit.test_normalization_service import GERMAN, page

NOW = datetime.now(UTC).replace(microsecond=0)
PAGE_TIME = "2026-10-22T08:00:00Z"


@pytest.fixture
def database(engine):
    upgrade(engine)
    return engine


def source(database, name="EZB", adapter_type="rss"):
    form = {"name": name, "adapter_type": adapter_type, "url": "" if adapter_type == "gdelt" else f"https://{name.lower()}.example/rss",
            "query": "zins" if adapter_type == "gdelt" else "", "is_active": "on", "fetch_interval_minutes": "60",
            "raw_text_retention_days": "30", "region": "europe"}
    return source_service.create(database, form).id


def article(database, source_id, key, raw=None, content_type="text/html; charset=utf-8", **fields):
    """An article whose full text is settled: stored with `raw`, else reserved."""
    row = {"source_id": source_id, "source_name": "Q", "source_url": "https://q.example/rss", "item_key": key,
           "url": f"https://q.example/{key}.html?utm_source=rss", "canonical_url": None, "title": f"Meldung {key}",
           "published_at": NOW, "published_at_source": "feed", "language": "en"} | fields
    with database.begin() as connection:
        [article_id] = article_repository.insert_new(connection, [row])
        if raw is None:
            article_repository.finish_full_text(connection, article_id, FullTextStatus.RESERVED, "vorbehalten")
        else:
            article_repository.finish_full_text(connection, article_id, FullTextStatus.STORED, None)
            raw_text_repository.insert_raw_text(connection, article_id, raw, content_type, row["url"].split("?")[0])
    return article_id


def normalize_all(database):
    normalization_service.enqueue_due(database)
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    handlers = {job_service.NORMALIZE_JOB_KIND: handlers[job_service.NORMALIZE_JOB_KIND]}
    while run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        pass


def stored(database, article_id):
    with database.connect() as connection:
        row = connection.execute(select(Article.__table__).where(Article.id == article_id)).one()
        plain = connection.execute(select(RawText.plain_text).where(RawText.article_id == article_id)).scalar_one_or_none()
    return row, plain


def test_a_page_is_cleaned_and_its_metadata_made_consistent(database):
    head = '<link rel="canonical" href="https://bank.example/news/a"><script type="application/ld+json">' \
           f'{{"@type": "NewsArticle", "datePublished": "{PAGE_TIME}"}}</script>'
    gdelt = source(database, "GDELT", "gdelt")
    article_id = article(database, gdelt, "a", page(head), title=None, published_at_source="gdelt", language=None)

    normalize_all(database)

    row, plain = stored(database, article_id)
    assert (row.normalization_status, row.normalization_error) == ("done", None)
    assert row.normalized_at is not None
    assert row.canonical_url == "https://bank.example/news/a"
    # GDELT only says when it saw the article; the page says when it was published.
    assert (row.published_at, row.published_at_source) == (datetime(2026, 10, 22, 8, tzinfo=UTC), "page")
    assert row.title == "Leitzins bleibt | Bank"
    assert row.detected_language == "de"
    assert "Leitzinsen am Donnerstag" in plain and "Impressum" not in plain
    assert row.url.endswith("?utm_source=rss")  # the link as the source gave it


def test_the_feed_s_time_stays_and_a_title_is_not_overwritten(database):
    head = f'<meta property="article:published_time" content="{PAGE_TIME}">'
    article_id = article(database, source(database), "a", page(head))

    normalize_all(database)

    row, _plain = stored(database, article_id)
    assert (row.published_at, row.published_at_source, row.title) == (NOW, "feed", "Meldung a")


def test_the_page_s_title_replaces_gdelt_s_tokenized_one(database):
    head = '<meta property="og:title" content="EZB: Leitzins bleibt bei 2.1 %">'
    gdelt = source(database, "GDELT", "gdelt")
    article_id = article(database, gdelt, "a", page(head), title="EZB : Leitzins bleibt bei 2 . 1 %", published_at_source="gdelt")

    normalize_all(database)

    assert stored(database, article_id)[0].title == "EZB: Leitzins bleibt bei 2.1 %"


def test_without_a_raw_text_only_the_link_is_made_canonical(database):
    article_id = article(database, source(database), "a")

    normalize_all(database)

    row, plain = stored(database, article_id)
    assert (row.normalization_status, row.canonical_url, row.detected_language, plain) == (
        "done", "https://q.example/a.html", None, None
    )


def test_a_raw_text_no_reader_understands_fails_once_and_is_not_queued_again(database):
    article_id = article(database, source(database), "a", b"%PDF-1.4 broken", "application/pdf")

    normalize_all(database)
    normalize_all(database)

    row, plain = stored(database, article_id)
    assert row.normalization_status == "failed" and row.normalization_error
    assert (row.canonical_url, plain) == ("https://q.example/a.html", None)
    jobs = [job for job in _jobs(database) if job.payload["article_id"] == article_id]
    assert [job.status for job in jobs] == [JobStatus.SUCCEEDED]


def test_due_are_settled_articles_once_each_a_backfill_s_behind_live_work(database):
    source_id = source(database)
    live = article(database, source_id, "live", page())
    with database.begin() as connection:
        connection.execute(text("INSERT INTO backfill (source_id, period_start, period_end, cursor) VALUES (:s, now() - interval '2 days', now(), now())"), {"s": source_id})
    old = article(database, source_id, "old", page(), backfill_id=None)
    with database.begin() as connection:
        connection.execute(update(Article).where(Article.id == old).values(backfill_id=text("(SELECT max(id) FROM backfill)")))
        # Its full text is still being fetched: not yet.
        [waiting] = article_repository.insert_new(connection, [{"source_id": source_id, "source_name": "Q", "source_url": "https://q.example/rss",
                                                                 "item_key": "waiting", "url": "https://q.example/w.html", "title": None,
                                                                 "published_at": None, "language": None}])

    first = normalization_service.enqueue_due(database)
    again = normalization_service.enqueue_due(database)

    assert len(first) == 2 and again == []
    priorities = {job.payload["article_id"]: job.priority for job in _jobs(database)}
    assert priorities == {live: 0, old: BACKFILL_PRIORITY}
    assert waiting not in priorities


def test_the_article_list_shows_what_normalization_made(database):
    article_id = article(database, source(database), "a", page(f'<meta property="article:published_time" content="{PAGE_TIME}">'), published_at=None, published_at_source=None)

    normalize_all(database)

    [shown] = article_service.overview(database, None).articles
    _row, plain = stored(database, article_id)
    assert shown.id == article_id
    assert (shown.language_label, shown.published_note) == ("Deutsch", "laut Seite")
    # The repeated paragraph of the page counts once.
    assert shown.text_chars == len(plain) and plain.count(GERMAN) == 1
    assert shown.text_note == f"Text {len(plain)}\u00a0Zeichen"


def _jobs(database):
    with database.connect() as connection:
        return connection.execute(select(Job.__table__).where(Job.kind == job_service.NORMALIZE_JOB_KIND).order_by(Job.id)).all()
