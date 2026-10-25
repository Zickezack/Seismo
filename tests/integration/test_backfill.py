"""Backfill in PostgreSQL: a period fetched page by page behind live work, its articles and full texts, cancelling
and ending it. The network is simulated (test_ingestion.Web)."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from sqlalchemy import select, text, update

from app.db.models import BACKFILL_PRIORITY, Backfill, BackfillStatus, FetchHost, Job, JobStatus
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.services import backfill_service, ingestion_service, job_service, source_service
from app.services.backfill_service import InvalidBackfill
from app.sources import ADAPTERS
from app.sources.gdelt import API_URL, GdeltAdapter
from app.sources.http import HttpClient
from app.sources.rss import RssAdapter
from tests.integration.alembic_helpers import upgrade
from tests.integration.test_ingestion import Web, articles, create, full_text, jobs, run_jobs

TODAY = datetime.now(UTC).date()
QUERY = '"central bank" sourcelang:german'


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
def gdelt(monkeypatch):
    """GDELT with pages of 3, so a test needs few articles for a full page."""
    adapter = GdeltAdapter()
    adapter.page_size = 3
    monkeypatch.setitem(ADAPTERS, "gdelt", adapter)
    return adapter


def gdelt_source(database, name="GDELT Zentralbanken"):
    return create(database, name, adapter_type="gdelt", url="", query=QUERY, language="")


def days_ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def start(database, source_id, first=10, last=1):
    return backfill_service.start(database, source_id, {"period_start": days_ago(first), "period_end": days_ago(last)})


def backfill_row(database, backfill_id):
    with database.connect() as connection:
        return connection.execute(select(Backfill.__table__).where(Backfill.id == backfill_id)).one()


def article_json(name, seen):
    return {"url": f"https://{name}.bank.example/{name}.html", "title": name, "language": "German",
            "seendate": seen.strftime("%Y%m%dT%H%M%SZ")}


def gdelt_pages(*pages):
    """Answers by the request's enddatetime: page n for the n-th distinct one asked for."""
    ends = []

    def answer(url):
        end = parse_qs(urlsplit(url).query)["enddatetime"][0]
        if end not in ends:
            ends.append(end)
        listed = pages[ends.index(end)] if ends.index(end) < len(pages) else []
        return 200, "application/json", json.dumps({"articles": listed}).encode()

    answer.ends = ends
    return answer


def later(database):
    """As it would be a while later: every host's interval has passed, every waiting job is due."""
    with database.begin() as connection:
        connection.execute(update(FetchHost).values(next_slot_at=text("now()"), last_request_at=text("now() - interval '1 minute'")))
        connection.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(run_after=text("now()")))


def test_gdelt_backfill_walks_back_through_its_period_page_by_page(database, web, gdelt):
    source_id = gdelt_source(database)
    backfill = start(database, source_id)
    end = backfill.period_end
    hour = timedelta(hours=1)
    pages = gdelt_pages(
        [article_json("a", end - hour), article_json("b", end - 2 * hour), article_json("c", end - 3 * hour)],
        # The oldest of the first page again: asked up to one second past it. Then less than a page: done.
        [article_json("c", end - 3 * hour), article_json("d", end - 4 * hour)],
    )
    web.pages[API_URL] = pages

    for _ in range(2):
        run_jobs(database, job_service.BACKFILL_JOB_KIND)
        later(database)

    row = backfill_row(database, backfill.id)
    assert (row.status, row.articles_found, row.articles_new, row.cursor) == (BackfillStatus.DONE, 5, 4, row.period_start)
    asked_end = (end - 3 * hour + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    assert pages.ends == [end.strftime("%Y%m%d%H%M%S"), asked_end]
    stored = articles(database)
    assert sorted(article.item_key.split("//")[1][0] for article in stored) == ["a", "b", "c", "d"]
    assert {article.backfill_id for article in stored} == {backfill.id}
    # Their full texts wait behind live work, with the same attempts as live ones.
    full_text_jobs = jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)
    assert {(job.priority, job.max_attempts) for job in full_text_jobs} == {(BACKFILL_PRIORITY, 31)}
    assert {job.priority for job in jobs(database, job_service.BACKFILL_JOB_KIND)} == {BACKFILL_PRIORITY}

    run_jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)
    [shown] = backfill_service.list_for_source(database, source_id)
    assert (shown.full_texts_stored, shown.full_texts_pending, shown.status_label) == (4, 0, "Abgeschlossen")


def test_an_article_known_from_the_live_fetch_counts_but_stays_live(database, web, gdelt):
    source_id = gdelt_source(database)
    seen = datetime.now(UTC) - timedelta(days=2)
    web.pages[API_URL] = (200, "application/json", json.dumps({"articles": [article_json("a", seen)]}).encode())
    ingestion_service.enqueue_due_fetches(database)
    run_jobs(database, job_service.FETCH_SOURCE_JOB_KIND)
    backfill = start(database, source_id)
    later(database)

    run_jobs(database, job_service.BACKFILL_JOB_KIND)

    row = backfill_row(database, backfill.id)
    assert (row.status, row.articles_found, row.articles_new) == (BackfillStatus.DONE, 1, 0)
    [article] = articles(database)
    assert article.backfill_id is None


def test_live_work_is_claimed_before_a_waiting_backfill(database, web, gdelt):
    web.pages[API_URL] = (200, "application/json", b"{}")
    start(database, gdelt_source(database))
    with database.begin() as connection:
        connection.execute(update(Job).values(run_after=text("now() - interval '1 hour'")))
    ingestion_service.enqueue_due_fetches(database)  # the source's live fetch, due only now
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")

    run_next_job(database, handlers, "test", 10, on_renewed=lambda: None)

    [live] = jobs(database)
    [waiting] = jobs(database, job_service.BACKFILL_JOB_KIND)
    assert (live.status, waiting.status, waiting.attempts) == (JobStatus.SUCCEEDED, JobStatus.QUEUED, 0)


def test_a_backfill_full_text_leaves_the_host_to_a_live_one(database, web, gdelt):
    source_id = gdelt_source(database)
    seen = datetime.now(UTC) - timedelta(days=5)
    same_host = {"url": "https://www.bank.example/alt.html", "title": "alt", "language": "German",
                 "seendate": seen.strftime("%Y%m%dT%H%M%SZ")}
    web.pages[API_URL] = (200, "application/json", json.dumps({"articles": [same_host]}).encode())
    start(database, source_id)
    run_jobs(database, job_service.BACKFILL_JOB_KIND)
    # A live article on the same host comes in, and the host has just been asked by someone else.
    fresh = {**same_host, "url": "https://www.bank.example/neu.html", "seendate": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")}
    web.pages[API_URL] = (200, "application/json", json.dumps({"articles": [fresh]}).encode())
    later(database)
    ingestion_service.enqueue_due_fetches(database)
    run_jobs(database, job_service.FETCH_SOURCE_JOB_KIND)
    with database.begin() as connection:
        connection.execute(update(FetchHost).values(next_slot_at=text("now()"), last_request_at=text("now() - interval '1 minute'")))

    run_jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)

    by_url = {article.url: article for article in articles(database)}
    assert full_text(database, by_url["https://www.bank.example/neu.html"].id) == ("stored", None)
    assert full_text(database, by_url["https://www.bank.example/alt.html"].id) == ("pending", None)
    waiting = next(job for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND) if job.status == JobStatus.QUEUED)
    # Back when the host is free, with nothing reserved; the next live request still gets the next slot.
    assert waiting.priority == BACKFILL_PRIORITY and "host_slot" not in waiting.payload
    assert timedelta(seconds=8) < waiting.run_after - datetime.now(UTC) <= timedelta(seconds=10)


def test_a_cancelled_backfill_sends_nothing_more(database, web, gdelt):
    web.pages[API_URL] = (200, "application/json", b"{}")
    source_id = gdelt_source(database)
    backfill = start(database, source_id)

    result = backfill_service.cancel(database, source_id, [backfill.id])
    run_jobs(database, job_service.BACKFILL_JOB_KIND)

    assert (result.processed, result.blocked) == (1, [])
    row = backfill_row(database, backfill.id)
    assert (row.status, row.finished_at is not None) == (BackfillStatus.CANCELLED, True)
    assert not any(url.startswith(API_URL) for url in web.requests)
    assert [job.status for job in jobs(database, job_service.BACKFILL_JOB_KIND)] == [JobStatus.SUCCEEDED]


def test_cancel_is_all_or_nothing_and_only_for_running_backfills_of_the_source(database, web, gdelt):
    web.pages[API_URL] = (200, "application/json", b"{}")
    source_id, other = gdelt_source(database), gdelt_source(database, "GDELT Andere")
    done, running = start(database, source_id), start(database, source_id)
    foreign = start(database, other)
    with database.begin() as connection:
        connection.execute(update(Backfill).where(Backfill.id == done.id).values(status=BackfillStatus.DONE))

    result = backfill_service.cancel(database, source_id, [done.id, running.id, foreign.id])

    assert result.processed == 0
    assert result.blocked == [f"Backfill #{done.id} (Abgeschlossen)", f"Backfill #{foreign.id} (nicht gefunden)"]
    assert backfill_row(database, running.id).status == BackfillStatus.RUNNING


def test_a_rejected_query_ends_the_backfill_with_the_reason(database, web, gdelt):
    web.pages[API_URL] = (200, "text/html", b"The specified phrase is too short.")
    backfill = start(database, gdelt_source(database))

    run_jobs(database, job_service.BACKFILL_JOB_KIND)

    row = backfill_row(database, backfill.id)
    assert (row.status, row.last_error) == (
        BackfillStatus.FAILED, "GDELT lehnt die Suchanfrage ab: The specified phrase is too short."
    )
    assert [job.status for job in jobs(database, job_service.BACKFILL_JOB_KIND)] == [JobStatus.SUCCEEDED]


def test_an_unreachable_source_is_retried_and_ends_the_backfill_on_the_last_attempt(database, web, gdelt):
    web.pages[API_URL] = (503, "text/html", b"down")
    backfill = start(database, gdelt_source(database))

    run_jobs(database, job_service.BACKFILL_JOB_KIND)
    [job] = jobs(database, job_service.BACKFILL_JOB_KIND)
    assert (job.status, backfill_row(database, backfill.id).status) == (JobStatus.QUEUED, BackfillStatus.RUNNING)

    with database.begin() as connection:
        connection.execute(update(Job).where(Job.id == job.id).values(attempts=job.max_attempts - 1))
    later(database)
    run_jobs(database, job_service.BACKFILL_JOB_KIND)

    assert (backfill_row(database, backfill.id).status, backfill_row(database, backfill.id).last_error) == (
        BackfillStatus.FAILED, "HTTP 503"
    )


def test_a_deactivated_source_ends_its_backfill_and_an_inactive_one_cannot_start(database, web, gdelt):
    web.pages[API_URL] = (200, "application/json", b"{}")
    source_id = gdelt_source(database)
    backfill = start(database, source_id)
    source_service.set_active(database, [source_id], False)

    run_jobs(database, job_service.BACKFILL_JOB_KIND)

    assert (backfill_row(database, backfill.id).status, backfill_row(database, backfill.id).last_error) == (
        BackfillStatus.CANCELLED, "Quelle inaktiv"
    )
    with pytest.raises(InvalidBackfill, match="^Die Quelle ist inaktiv"):
        start(database, source_id)


def test_rss_backfill_takes_the_older_feed_items_within_the_period_in_one_request(database, web, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "rss", RssAdapter())
    old, older = datetime.now(UTC) - timedelta(days=12), datetime.now(UTC) - timedelta(days=40)
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Bank</title>
<item><guid>old</guid><link>https://www.bank.example/old.html</link><pubDate>{old:%a, %d %b %Y %H:%M:%S} +0000</pubDate></item>
<item><guid>older</guid><link>https://www.bank.example/older.html</link><pubDate>{older:%a, %d %b %Y %H:%M:%S} +0000</pubDate></item>
</channel></rss>"""
    web.pages["https://ezb.example/rss"] = (200, "application/rss+xml", feed.encode())
    source_id = create(database, "EZB")
    backfill = start(database, source_id, first=30, last=1)

    run_jobs(database, job_service.BACKFILL_JOB_KIND)

    assert [article.url for article in articles(database)] == ["https://www.bank.example/old.html"]
    assert backfill_row(database, backfill.id).status == BackfillStatus.DONE
    assert web.requests.count("https://ezb.example/rss") == 1


def test_deleting_the_source_deletes_its_backfills_and_keeps_their_articles(database, web, gdelt):
    seen = datetime.now(UTC) - timedelta(days=5)
    web.pages[API_URL] = (200, "application/json", json.dumps({"articles": [article_json("a", seen)]}).encode())
    source_id = gdelt_source(database)
    backfill = start(database, source_id)
    run_jobs(database, job_service.BACKFILL_JOB_KIND)

    source_service.delete(database, [source_id])

    with database.connect() as connection:
        assert connection.execute(select(Backfill.id)).all() == []
    [article] = articles(database)
    assert (article.source_id, article.backfill_id) == (None, None)
    assert backfill.id


def test_a_source_without_backfill_refuses_one(database):
    eia = create(
        database, "EIA Gasspeicher", adapter_type="eia",
        url="https://api.eia.gov/v2/natural-gas/stor/wkly/data/?frequency=weekly&data[0]=value", language="",
    )

    assert not backfill_service.offered(source_service.get(database, eia))
    with pytest.raises(InvalidBackfill, match="^Quellen vom Typ EIA haben keinen Backfill.$"):
        start(database, eia)
    with database.connect() as connection:
        assert connection.execute(select(Backfill.id)).all() == []
