"""Live ingestion in PostgreSQL: due sources are queued, fetch jobs store articles once, articles outlive their source."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select, text, update

from app.db.models import Article, Job, JobStatus, Source
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.repositories import platform_settings_repository
from app.services import article_service, ingestion_service, job_service, source_service
from app.sources import ADAPTERS
from app.sources.base import FetchedItem
from app.sources.errors import SourceRejected, SourceUnavailable
from app.sources.http import HttpClient
from tests.integration.alembic_helpers import upgrade

FEEDS = Path(__file__).parents[1] / "unit" / "feeds"
NOW = datetime.now(UTC).replace(microsecond=0)


class FakeAdapter:
    """Returns `items`, or raises `error`; records each fetch."""

    label = "RSS"

    def __init__(self):
        self.items, self.error, self.fetches = [], None, 0

    def fetch(self, source, http):
        self.fetches += 1
        if self.error:
            raise self.error
        return list(self.items)


@pytest.fixture
def database(engine):
    upgrade(engine)
    return engine


@pytest.fixture
def adapter(monkeypatch):
    fake = FakeAdapter()
    monkeypatch.setitem(ADAPTERS, "rss", fake)
    return fake


def item(key, published_at=NOW, **fields):
    values = dict(key=key, url=f"https://bank.example/{key}.html", title=f"Meldung {key}", published_at=published_at, language=None)
    return FetchedItem(**values | fields)


def create(database, name, **fields):
    form = {
        "name": name, "adapter_type": "rss", "url": f"https://{name.lower()}.example/rss", "is_active": "on",
        "fetch_interval_minutes": "60", "language": "en", "terms_note": "", "raw_text_retention_days": "30",
    }
    return source_service.create(database, form | fields).id


def run_jobs(database):
    """Run every due job with the worker's handlers; the number run."""
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    count = 0
    while run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        count += 1
    return count


def articles(database):
    with database.connect() as connection:
        return connection.execute(select(Article.__table__).order_by(Article.id)).all()


def jobs(database):
    with database.connect() as connection:
        return connection.execute(select(Job.__table__).order_by(Job.id)).all()


def make_due(database, source_id):
    with database.begin() as connection:
        connection.execute(update(Source).where(Source.id == source_id).values(next_fetch_at=text("now() - interval '1 second'")))


def test_due_active_sources_are_queued_once_per_interval(database, adapter):
    ezb = create(database, "EZB")
    create(database, "Fed", is_active="")

    first = ingestion_service.enqueue_due_fetches(database)
    second = ingestion_service.enqueue_due_fetches(database)

    assert len(first) == 1 and second == []
    [job] = jobs(database)
    assert (job.kind, job.payload, job.dedupe_key) == ("fetch_source", {"source_id": ezb}, f"fetch_source:{ezb}")
    with database.connect() as connection:
        due_in = connection.execute(select(Source.next_fetch_at - text("now()")).where(Source.id == ezb)).scalar_one()
    assert timedelta(minutes=59) < due_in <= timedelta(minutes=60)


def test_a_waiting_fetch_is_not_queued_twice(database, adapter):
    ezb = create(database, "EZB")
    ingestion_service.enqueue_due_fetches(database)
    make_due(database, ezb)

    assert ingestion_service.enqueue_due_fetches(database) == []
    assert len(jobs(database)) == 1


def test_fetch_job_stores_new_articles_once_with_a_copy_of_the_source(database, adapter):
    ezb = create(database, "EZB")
    adapter.items = [item("a", language="de"), item("b", published_at=None)]
    ingestion_service.enqueue_due_fetches(database)

    assert run_jobs(database) == 1

    stored = articles(database)
    assert [(row.item_key, row.source_id, row.source_name, row.source_url, row.language) for row in stored] == [
        ("a", ezb, "EZB", "https://ezb.example/rss", "de"),
        ("b", ezb, "EZB", "https://ezb.example/rss", "en"),
    ]
    assert stored[0].published_at == NOW and stored[1].published_at is None
    assert stored[0].fetched_at > NOW - timedelta(minutes=1)
    source = source_service.get(database, ezb)
    assert source.last_fetched_at is not None
    assert source.updated_at < source.last_fetched_at  # a fetch is no change by the admin

    # The next fetch finds the same items again plus a new one.
    adapter.items.append(item("c"))
    make_due(database, ezb)
    ingestion_service.enqueue_due_fetches(database)
    run_jobs(database)

    assert [row.item_key for row in articles(database)] == ["a", "b", "c"]
    assert [job.status for job in jobs(database)] == [JobStatus.SUCCEEDED, JobStatus.SUCCEEDED]


def test_items_older_than_the_maximum_age_are_skipped(database, adapter):
    with database.begin() as connection:
        platform_settings_repository.save(connection, {"article_max_age_days": 2})
    create(database, "EZB")
    adapter.items = [item("new", published_at=NOW - timedelta(days=1)), item("old", published_at=NOW - timedelta(days=3))]
    ingestion_service.enqueue_due_fetches(database)

    run_jobs(database)

    assert [row.item_key for row in articles(database)] == ["new"]


def test_rejected_source_fails_at_once_with_its_message(database, adapter):
    create(database, "EZB")
    adapter.error = SourceRejected("HTTP 404")
    ingestion_service.enqueue_due_fetches(database)

    run_jobs(database)

    [job] = jobs(database)
    assert (job.status, job.attempts, job.last_error) == (JobStatus.FAILED, 1, "PermanentJobError: HTTP 404")


def test_unreachable_source_is_retried_later(database, adapter):
    create(database, "EZB")
    adapter.error = SourceUnavailable("HTTP 503")
    ingestion_service.enqueue_due_fetches(database)

    run_jobs(database)

    [job] = jobs(database)
    assert (job.status, job.attempts, job.last_error) == (JobStatus.QUEUED, 1, "SourceUnavailable: HTTP 503")
    assert articles(database) == []


@pytest.mark.parametrize("change", ["delete", "deactivate"])
def test_source_deleted_or_deactivated_after_queueing_is_not_fetched(database, adapter, change):
    ezb = create(database, "EZB")
    adapter.items = [item("a")]
    ingestion_service.enqueue_due_fetches(database)
    if change == "delete":
        source_service.delete(database, [ezb])
    else:
        source_service.set_active(database, [ezb], False)

    run_jobs(database)

    assert adapter.fetches == 0
    assert articles(database) == []
    assert [job.status for job in jobs(database)] == [JobStatus.SUCCEEDED]


def test_articles_keep_name_and_url_of_a_deleted_source_and_show_a_renamed_one_by_its_new_name(database, adapter):
    ezb, fed = create(database, "EZB"), create(database, "Fed")
    adapter.items = [item("a")]
    ingestion_service.enqueue_due_fetches(database)
    run_jobs(database)

    source_service.delete(database, [ezb])
    source_service.update(database, fed, {**_form(source_service.get(database, fed)), "name": "Federal Reserve"})

    stored = articles(database)
    assert [(row.source_id, row.source_name) for row in stored] == [(None, "EZB"), (fed, "Fed")]
    shown = {(entry.source_display_name, entry.source_exists) for entry in article_service.overview(database, None).articles}
    assert shown == {("EZB", False), ("Federal Reserve", True)}


def test_fetch_now_queues_at_once_and_is_all_or_nothing(database, adapter):
    ezb, fed = create(database, "EZB"), create(database, "Fed", is_active="")

    blocked = ingestion_service.fetch_now(database, [ezb, fed, 404])

    assert blocked.processed == 0
    assert blocked.blocked == ["Fed (inaktiv)", "Quelle #404 (nicht gefunden)"]
    assert jobs(database) == []

    done = ingestion_service.fetch_now(database, [ezb])

    assert (done.processed, len(jobs(database))) == (1, 1)
    # Its next regular fetch is one interval later, not at once.
    assert ingestion_service.enqueue_due_fetches(database) == []


def test_overview_filters_by_source_and_counts(database, adapter):
    ezb, fed = create(database, "EZB"), create(database, "Fed")
    adapter.items = [item("a"), item("b")]
    ingestion_service.enqueue_due_fetches(database)
    run_jobs(database)

    assert article_service.count_by_source(database) == {ezb: 2, fed: 2}
    filtered = article_service.overview(database, str(fed))
    assert filtered.source_id == fed and filtered.matching == 2
    assert {entry.source_id for entry in filtered.articles} == {fed}
    assert article_service.overview(database, "not-a-source").source_id is None
    assert article_service.overview(database, None).matching == 4


def test_end_to_end_with_the_rss_adapter_over_http(database, monkeypatch):
    """The real adapter and HTTP client, with the network replaced by the sample feed."""
    feed = (FEEDS / "rss2.xml").read_bytes()
    seen = []

    def answer(request):
        seen.append(request.headers["user-agent"])
        return httpx2.Response(200, content=feed)

    monkeypatch.setattr(ingestion_service, "HttpClient", lambda contact: HttpClient(contact, httpx2.MockTransport(answer)))
    with database.begin() as connection:
        platform_settings_repository.save(connection, {"article_max_age_days": 365})
    create(database, "Beispielbank", url="https://bank.example/rss/press.xml")
    ingestion_service.enqueue_due_fetches(database)

    run_jobs(database)

    assert seen[0].endswith("(+ops@example.org)")
    stored = articles(database)
    assert [row.url for row in stored] == [
        "https://bank.example/press/2026-10-23.html",
        "https://bank.example/press/relative.html",
        "https://bank.example/press/undated.html",
    ]
    assert [row.language for row in stored] == ["de", "de", "de"]


def _form(source):
    return {
        "name": source.name, "adapter_type": source.adapter_type, "url": source.url,
        "is_active": "on" if source.is_active else "", "fetch_interval_minutes": str(source.fetch_interval_minutes),
        "language": source.language or "", "terms_note": source.terms_note or "",
        "raw_text_retention_days": str(source.raw_text_retention_days),
    }
