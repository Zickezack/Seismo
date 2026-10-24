"""Duplicates in PostgreSQL: the same link at ingestion, live and in a backfill, the same text or the page's own
canonical link at normalization; the original is the article published first. The network is simulated
(test_ingestion.Web)."""
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import text

from app.db.models import BACKFILL_PRIORITY
from app.repositories.article_repository import DUPLICATE_LOCK_NAMESPACE
from app.services import backfill_service, duplicate_service, ingestion_service, job_service
from app.sources import ADAPTERS
from app.sources.gdelt import API_URL
from app.sources.http import HttpClient
from tests.integration.alembic_helpers import upgrade
from tests.integration.test_backfill import article_json, gdelt, gdelt_pages, gdelt_source, later, start  # noqa: F401
from tests.integration.test_ingestion import NOW, FakeAdapter, Web, articles, create, item, jobs, run_jobs
from tests.integration.test_normalization import article, normalize_all, source
from tests.unit.test_normalization_service import page

LINK = "https://a.bank.example/a.html"


@pytest.fixture
def web(monkeypatch):
    fake = Web()
    client = lambda contact: HttpClient(contact, httpx2.MockTransport(fake))  # noqa: E731
    monkeypatch.setattr(ingestion_service, "HttpClient", client)
    monkeypatch.setattr(backfill_service, "HttpClient", client)
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


def fetch(database, adapter, name, *items):
    """A new source of that name fetches the items once."""
    source_id = create(database, name)
    adapter.items = list(items)
    ingestion_service.fetch_now(database, [source_id])
    run_jobs(database, job_service.FETCH_SOURCE_JOB_KIND)
    return source_id


def full_text_jobs(database):
    return [job.payload["article_id"] for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)]


def groups(database):
    return [(row.duplicate_of_id, row.duplicate_reason, row.full_text_status) for row in articles(database)]


def test_a_new_article_with_a_stored_link_is_a_duplicate_without_a_full_text_of_its_own(database, web, adapter):
    fetch(database, adapter, "Bank", item("a"))
    # Another link to the same page: tracking parameters do not make it another one.
    fetch(database, adapter, "Presse", item("b", url=f"{LINK}?utm_source=rss", published_at=NOW + timedelta(minutes=5)))

    a, _b = articles(database)
    assert groups(database) == [(None, None, "pending"), (a.id, "url", "skipped")]
    assert full_text_jobs(database) == [a.id]


def test_an_article_found_later_but_published_earlier_leads_the_group(database, web, adapter):
    fetch(database, adapter, "Bank", item("a", url=LINK))
    run_jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)
    fetch(database, adapter, "Presse", item("b", url=LINK, published_at=NOW + timedelta(minutes=5)))
    fetch(database, adapter, "Agentur", item("c", url=LINK, published_at=NOW - timedelta(hours=1)))

    a, _b, c = articles(database)
    # The full text stored before stays; the new original fetches its own.
    assert groups(database) == [(c.id, "url", "stored"), (c.id, "url", "skipped"), (None, None, "pending")]
    assert full_text_jobs(database) == [a.id, c.id]


def test_an_unknown_time_never_comes_first_and_on_equal_times_the_first_stored_stays_original(database, web, adapter):
    fetch(database, adapter, "Bank", item("a", url=LINK, published_at=None))
    fetch(database, adapter, "Presse", item("b", url=LINK))
    fetch(database, adapter, "Agentur", item("c", url=LINK))

    _a, b, _c = articles(database)
    assert [row[:2] for row in groups(database)] == [(b.id, "url"), (None, None), (b.id, "url")]


def test_a_backfill_s_article_published_earlier_becomes_the_original_with_a_backfill_full_text(database, web, adapter, gdelt):
    fetch(database, adapter, "Bank", item("a", url=LINK))
    backfill = start(database, gdelt_source(database))
    web.pages[API_URL] = gdelt_pages([article_json("a", backfill.period_end - timedelta(hours=1))])
    later(database)

    run_jobs(database, job_service.BACKFILL_JOB_KIND)

    a, found = articles(database)
    assert (found.backfill_id, found.url) == (backfill.id, LINK)
    assert groups(database) == [(found.id, "url", "skipped"), (None, None, "pending")]
    [_live, backfill_job] = jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)
    assert (backfill_job.payload["article_id"], backfill_job.priority) == (found.id, BACKFILL_PRIORITY)


def test_the_same_text_on_another_host_is_a_duplicate_of_the_one_published_first(database):
    source_id = source(database)
    a = article(database, source_id, "a", page(), url="https://a.example/a.html")
    b = article(database, source_id, "b", page(), url="https://b.example/b.html", published_at=NOW - timedelta(hours=1))

    normalize_all(database)

    rows = {row.id: row for row in articles(database)}
    assert (rows[a].duplicate_of_id, rows[a].duplicate_reason, rows[b].duplicate_of_id) == (b, "text", None)
    assert rows[a].content_hash == rows[b].content_hash is not None
    # Both pages were fetched: identical text skips nothing.
    assert (rows[a].full_text_status, rows[b].full_text_status) == ("stored", "stored")


def test_the_same_text_on_one_host_under_another_canonical_url_is_no_duplicate(database):
    source_id = source(database)
    # By the publisher's own word two pages: the text is no report, e.g. an error page.
    article(database, source_id, "a", page(), url="https://q.example/a.html")
    article(database, source_id, "b", page(), url="https://q.example/b.html")

    normalize_all(database)

    a, b = articles(database)
    assert (a.duplicate_of_id, b.duplicate_of_id) == (None, None)
    assert a.content_hash == b.content_hash is not None


def test_a_short_text_has_no_fingerprint_and_makes_no_duplicates(database):
    source_id = source(database)
    short = page(body="<article><h1>Auktion</h1><p>Aufstockung der zehnjährigen Bundesanleihe.</p></article>")
    article(database, source_id, "a", short, url="https://a.example/a.html")
    article(database, source_id, "b", short, url="https://b.example/b.html")

    normalize_all(database)

    assert [(row.content_hash, row.duplicate_of_id) for row in articles(database)] == [(None, None), (None, None)]


def test_the_page_s_canonical_link_takes_an_article_and_its_duplicates_to_that_link_s_original(database):
    source_id = source(database)
    agency_link = "https://agency.example/story"
    agency = article(database, source_id, "agency", url=agency_link, canonical_url=agency_link, published_at=NOW - timedelta(hours=2))
    local_link = "https://local.example/ap/story"
    local_page = page(f'<link rel="canonical" href="{agency_link}">')
    local = article(database, source_id, "local", local_page, url=local_link, canonical_url=local_link)
    again = article(database, source_id, "again", url=local_link, canonical_url=local_link)
    with database.begin() as connection:
        duplicate_service.mark_new(connection, [local, again])

    normalize_all(database)

    rows = {row.id: row for row in articles(database)}
    # Never a duplicate of a duplicate: the local copy's own duplicate follows it.
    assert [(rows[key].duplicate_of_id, rows[key].duplicate_reason) for key in (agency, local, again)] == [
        (None, None), (agency, "url"), (agency, "url")
    ]


def test_one_check_at_a_time_until_its_transaction_ends(database):
    try_lock = text(f"SELECT pg_try_advisory_xact_lock({DUPLICATE_LOCK_NAMESPACE}, 0)")
    with database.begin() as first:
        duplicate_service.lock(first)
        with database.begin() as second:
            assert second.execute(try_lock).scalar_one() is False

    with database.begin() as third:
        assert third.execute(try_lock).scalar_one() is True
