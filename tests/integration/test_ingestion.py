"""Live ingestion in PostgreSQL: due sources are queued, fetch jobs store articles once, articles outlive their
source, and each new article's full text lands in the raw-data layer. The network is simulated (Web)."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import cast, select, text, update
from sqlalchemy.dialects.postgresql import JSONB

from app.db.models import Article, FetchHost, Job, JobStatus, RawText, Source
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.repositories import platform_settings_repository
from app.services import article_service, ingestion_service, job_service, source_service
from app.sources import ADAPTERS
from app.sources.base import FetchedItem
from app.sources.errors import SourceRejected, SourceUnavailable
from app.sources.http import HttpClient
from app.sources.rss import RssAdapter
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


class Web:
    """The network: robots.txt per host and answers per URL; any other page is a small HTML page."""

    def __init__(self):
        self.robots, self.pages, self.requests = {}, {}, []

    def __call__(self, request):
        url = str(request.url)
        self.requests.append(url)
        if request.url.path == "/robots.txt":
            status, body = self.robots.get(request.url.host, (404, ""))
            return httpx2.Response(status, text=body)
        status, content_type, body = self.pages.get(url, (200, "text/html; charset=utf-8", f"<p>{url}</p>".encode()))
        return httpx2.Response(status, content=body, headers={"content-type": content_type} if content_type else {})


@pytest.fixture(autouse=True)
def web(monkeypatch):
    fake = Web()
    monkeypatch.setattr(ingestion_service, "HttpClient", lambda contact: HttpClient(contact, httpx2.MockTransport(fake)))
    return fake


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
    # A host per item: no item waits for another's request slot.
    url = f"https://{key}.bank.example/{key}.html"
    values = dict(key=key, url=url, title=f"Meldung {key}", published_at=published_at, language=None)
    return FetchedItem(**values | fields)


def create(database, name, **fields):
    form = {
        "name": name, "adapter_type": "rss", "url": f"https://{name.lower()}.example/rss", "is_active": "on",
        "fetch_interval_minutes": "60", "language": "en", "terms_note": "", "raw_text_retention_days": "30",
    }
    return source_service.create(database, form | fields).id


def run_jobs(database, *kinds):
    """Run every due job of these kinds (default: all) with the worker's handlers; the number run."""
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    handlers = {kind: handler for kind, handler in handlers.items() if not kinds or kind in kinds}
    count = 0
    while run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        count += 1
    return count


def articles(database):
    with database.connect() as connection:
        return connection.execute(select(Article.__table__).order_by(Article.id)).all()


def jobs(database, kind=job_service.FETCH_SOURCE_JOB_KIND):
    with database.connect() as connection:
        return connection.execute(select(Job.__table__).where(Job.kind == kind).order_by(Job.id)).all()


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

    assert run_jobs(database, job_service.FETCH_SOURCE_JOB_KIND) == 1

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
    run_jobs(database, job_service.FETCH_SOURCE_JOB_KIND)

    assert [row.item_key for row in articles(database)] == ["a", "b", "c"]
    # One full-text job per new article, none for the ones found again.
    assert [job.payload for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)] == [
        {"article_id": row.id} for row in articles(database)
    ]
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


def raw_texts(database):
    with database.connect() as connection:
        return connection.execute(select(RawText.__table__).order_by(RawText.article_id)).all()


def fetch_articles(database, adapter, *items):
    """Store these items as new articles of a fresh source "EZB"; their full-text jobs wait in the queue."""
    create(database, "EZB")
    adapter.items = list(items)
    ingestion_service.enqueue_due_fetches(database)
    run_jobs(database, job_service.FETCH_SOURCE_JOB_KIND)
    return {row.item_key: row.id for row in articles(database)}


def full_text(database, article_id):
    with database.connect() as connection:
        row = connection.execute(select(Article.full_text_status, Article.full_text_error).where(Article.id == article_id)).one()
    return tuple(row)


def test_full_text_is_kept_as_fetched_with_its_type_and_final_url(database, adapter, web):
    web.pages["https://a.bank.example/a.html"] = (200, "text/html; charset=iso-8859-1", "<p>Zürich</p>".encode("latin-1"))
    ids = fetch_articles(database, adapter, item("a"))

    run_jobs(database)

    assert full_text(database, ids["a"]) == ("stored", None)
    [raw] = raw_texts(database)
    assert (raw.article_id, raw.content, raw.content_type, raw.url) == (
        ids["a"], "<p>Zürich</p>".encode("latin-1"), "text/html; charset=iso-8859-1", "https://a.bank.example/a.html"
    )
    [shown] = article_service.overview(database, None).articles
    assert (shown.full_text_status, shown.full_text_bytes) == ("stored", len("<p>Zürich</p>"))
    assert [job.status for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)] == [JobStatus.SUCCEEDED]


@pytest.mark.parametrize(
    ("robots", "url_allowed"),
    [
        ((200, "User-agent: *\nDisallow: /a.html\n"), False),
        ((200, "User-agent: *\nAllow: /\n\nUser-agent: Seismo\nDisallow: /\n"), False),
        ((200, "User-agent: GPTBot\nDisallow: /\n"), True),
        ((403, "forbidden"), False),
        ((401, ""), False),
        ((404, ""), True),
        ((410, ""), True),
    ],
)
def test_robots_txt_decides(database, adapter, web, robots, url_allowed):
    web.robots["a.bank.example"] = robots
    ids = fetch_articles(database, adapter, item("a"))

    run_jobs(database)

    expected = ("stored", None) if url_allowed else ("disallowed", "Durch robots.txt gesperrt")
    assert full_text(database, ids["a"]) == expected
    assert ("https://a.bank.example/a.html" in web.requests) is url_allowed


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ((200, "image/png", b"\x89PNG"), ("unsupported", "Inhaltstyp image/png wird nicht gespeichert")),
        ((200, None, b"?"), ("unsupported", "Inhaltstyp unbekannt wird nicht gespeichert")),
        ((404, "text/html", b"weg"), ("failed", "HTTP 404")),
        ((200, "application/pdf", b"%PDF-1.7"), ("stored", None)),
    ],
)
def test_lasting_outcomes_settle_the_article(database, adapter, web, answer, outcome):
    web.pages["https://a.bank.example/a.html"] = answer
    ids = fetch_articles(database, adapter, item("a"))

    run_jobs(database)

    assert full_text(database, ids["a"]) == outcome
    assert [job.status for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)] == [JobStatus.SUCCEEDED]


def test_passing_failure_is_retried_and_settles_as_failed_on_the_last_attempt(database, adapter, web):
    web.pages["https://a.bank.example/a.html"] = (503, "text/html", b"busy")
    ids = fetch_articles(database, adapter, item("a"))

    run_jobs(database)

    [job] = jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)
    assert (job.status, job.attempts, full_text(database, ids["a"])) == (JobStatus.QUEUED, 1, ("pending", None))

    with database.begin() as connection:
        connection.execute(update(Job).where(Job.id == job.id).values(attempts=job.max_attempts - 1, run_after=text("now()")))
        # A retry keeps the host's minimum interval as well; here it has passed.
        connection.execute(update(FetchHost).values(next_slot_at=text("now()"), last_request_at=text("now() - interval '1 minute'")))
    run_jobs(database)

    assert full_text(database, ids["a"]) == ("failed", "HTTP 503")
    assert jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)[0].status == JobStatus.SUCCEEDED


@pytest.mark.parametrize(("change", "reason"), [("deactivate", "Quelle inaktiv"), ("delete", "Quelle gelöscht")])
def test_full_text_of_an_inactive_or_deleted_source_is_skipped(database, adapter, web, change, reason):
    ids = fetch_articles(database, adapter, item("a"))
    [ezb] = [source.id for source in source_service.list_sources(database)]
    if change == "delete":
        source_service.delete(database, [ezb])
    else:
        source_service.set_active(database, [ezb], False)

    run_jobs(database)

    assert full_text(database, ids["a"]) == ("skipped", reason)
    assert "https://a.bank.example/a.html" not in web.requests


def test_feed_disallowed_by_robots_txt_fails_its_job(database, adapter, web, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "rss", RssAdapter())
    web.robots["ezb.example"] = (200, "User-agent: *\nDisallow: /rss\n")
    create(database, "EZB")
    ingestion_service.enqueue_due_fetches(database)

    run_jobs(database)

    [job] = jobs(database)
    assert (job.status, job.last_error) == (JobStatus.FAILED, "PermanentJobError: Durch robots.txt gesperrt")
    assert web.requests == ["https://ezb.example/robots.txt"]


def test_requests_to_one_host_keep_the_minimum_interval(database, adapter, web):
    same_host = {"url": "https://www.bank.example/{}.html"}
    ids = fetch_articles(
        database, adapter, item("a", url=same_host["url"].format("a")), item("b", url=same_host["url"].format("b"))
    )

    run_jobs(database)

    # The first goes at once; the second is deferred to its reserved slot, 10 s later, without counting an attempt.
    statuses = {key: full_text(database, article_id)[0] for key, article_id in ids.items()}
    assert sorted(statuses.values()) == ["pending", "stored"]
    waiting = next(job for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND) if job.status == JobStatus.QUEUED)
    slot = datetime.fromisoformat(waiting.payload["host_slot"]["at"])
    assert waiting.attempts == 0 and waiting.run_after == slot
    assert waiting.payload["host_slot"]["attempt"] == 1  # the attempt it comes back with
    assert timedelta(seconds=9) < slot - datetime.now(UTC) <= timedelta(seconds=10)
    assert web.requests.count("https://www.bank.example/robots.txt") == 1

    # At its slot it goes without reserving again (the previous request is moved back instead of waiting).
    with database.begin() as connection:
        connection.execute(update(Job).where(Job.id == waiting.id).values(run_after=text("now()")))
        connection.execute(update(FetchHost).values(last_request_at=text("now() - interval '1 minute'")))
    run_jobs(database)

    assert sorted(full_text(database, article_id)[0] for article_id in ids.values()) == ["stored", "stored"]
    assert web.requests.count("https://www.bank.example/robots.txt") == 1


def test_a_retry_queues_up_again_instead_of_using_its_old_slot(database, adapter, web):
    url = "https://www.bank.example/{}.html"
    web.pages[url.format("a")] = (503, "text/html", b"busy")
    ids = fetch_articles(database, adapter, item("a", url=url.format("a")), item("b", url=url.format("b")))
    run_jobs(database)  # a: sent, 503; b: deferred to its slot
    job_a = next(job for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND) if job.payload["article_id"] == ids["a"])
    with database.begin() as connection:
        # a's retry is due; give it an old slot as if it had been deferred before its first attempt.
        old_slot = {"host_slot": {"at": "2026-01-01T00:00:00+00:00", "attempt": 1}}
        connection.execute(update(Job).where(Job.id == job_a.id).values(run_after=text("now()"), payload=Job.payload.op("||")(cast(old_slot, JSONB))))

    run_jobs(database)

    # The retry (attempt 2) reserved a new slot behind b's and was deferred; nothing was sent.
    retried = next(job for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND) if job.id == job_a.id)
    assert retried.payload["host_slot"]["attempt"] == 2
    assert timedelta(seconds=19) < retried.run_after - datetime.now(UTC) <= timedelta(seconds=20)
    assert web.requests.count(url.format("a")) == 1
