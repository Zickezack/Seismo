"""Event identity in PostgreSQL: which event a candidate joins or founds, by its words or by the LLM, within the time
an event stays open - against a simulated Ollama."""
import json
import logging
import threading
from datetime import timedelta
from functools import partial
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select

from app.db.models import Event, EventCandidate, JobStatus
from app.jobs.errors import JobDeferred
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import extraction_repository
from app.services import event_service, job_service
from app.services.event_service import ASSIGN_FROM, NEW_BELOW
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_extraction import Ollama, refused, source
from tests.integration.test_ingestion import NOW, jobs
from tests.integration.test_normalization import article

KIND = job_service.ASSIGN_EVENT_JOB_KIND
QUAKE = {"title": "Earthquake of magnitude 6.8 strikes Xizang", "place": "Dingri", "actors": ["China Earthquake Networks Center"],
         "summary": "A strong earthquake hit Dingri county in Xizang, killing 126 people.", "event_type": "natural_disaster", "country": "CN"}
SAME_QUAKE = QUAKE | {"title": "Magnitude 6.8 earthquake strikes Xizang, killing 126",
                      "summary": "An earthquake of magnitude 6.8 hit Dingri county in Xizang; 126 people were killed."}
RATES = {"title": "ECB keeps key interest rates unchanged", "place": "Frankfurt", "actors": ["ECB"], "event_type": "monetary_policy",
         "summary": "The European Central Bank left its key interest rates unchanged on Thursday.", "country": None}
# Next to QUAKE and RATES in the grey zone of QUAKE: a continuation in words of its own.
RESCUE = {"title": "Xizang earthquake: rescue continues in Dingri", "place": "Dingri", "actors": [], "event_type": "natural_disaster",
          "summary": "Rescuers search the rubble in Dingri county after the earthquake in Xizang.", "country": "CN"}


def reported(database, key, *reports, published_at=NOW):
    """An article of `published_at` whose extraction found these reports; their candidate ids."""
    article_id = article(database, source(database, f"Q{key}"), key, published_at=published_at, url=f"https://news.example/{key}.html")
    candidates = [
        {"event_type": each["event_type"], "status": "start", "title": each["title"], "summary": each["summary"],
         "country": each["country"], "place": each["place"], "actors": [{"name": name, "role": "", "quote": name} for name in each["actors"]],
         "impacts": [], "statements": []}
        for each in reports
    ]
    values = {"article_id": article_id, "model": "m", "schema_version": "2", "duration_ms": 1, "input_chars": 1, "truncated": False}
    with database.begin() as connection:
        extraction_id = extraction_repository.insert_extraction(connection, values, candidates)
        return list(connection.execute(
            select(EventCandidate.id).where(EventCandidate.extraction_id == extraction_id).order_by(EventCandidate.position)
        ).scalars())


def llm(ollama):
    return partial(OllamaClient, "http://ollama:11434", "qwen3.5:9b", 8192, transport=httpx2.MockTransport(ollama))


def assign_all(database, ollama, jobs_to_run=None):
    """Queue the due assignments and run them, or as many as `jobs_to_run`."""
    event_service.enqueue_due(database)
    stop = threading.Event()
    handlers = build_handlers(database, Path("/nonexistent"), LlmCallLimiter(database, stop), stop, "ops@example.org", llm(ollama))
    handlers = {KIND: handlers[KIND]}
    run = 0
    while (jobs_to_run is None or run < jobs_to_run) and run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        run += 1


def assigned(database):
    with database.connect() as connection:
        rows = connection.execute(select(EventCandidate.__table__)).all()
    return {row.id: row for row in rows}


def event(database, event_id):
    with database.connect() as connection:
        return connection.execute(select(Event.__table__).where(Event.id == event_id)).one()


def test_the_earliest_report_founds_the_event_and_a_similar_one_joins_it_by_its_words(database):
    [later] = reported(database, "a", SAME_QUAKE)
    [earlier] = reported(database, "b", QUAKE, published_at=NOW - timedelta(hours=2))
    [rates] = reported(database, "c", RATES, published_at=NOW - timedelta(hours=1))
    # Never asked: the scores decide alone.
    ollama = Ollama(status=503)

    assign_all(database, ollama)

    rows = assigned(database)
    assert (rows[earlier].assignment_method, rows[earlier].assignment_score) == ("new", None)
    assert (rows[later].event_id, rows[later].assignment_method) == (rows[earlier].event_id, "score")
    assert rows[later].assignment_score >= ASSIGN_FROM and rows[later].assigned_at is not None
    assert rows[rates].event_id != rows[earlier].event_id and rows[rates].assignment_method == "new"
    assert ollama.requests == []
    quake = event(database, rows[earlier].event_id)
    assert (quake.title, quake.event_type, quake.country) == (QUAKE["title"], "natural_disaster", "CN")
    assert (quake.first_reported_at, quake.last_reported_at) == (NOW - timedelta(hours=2), NOW)
    detail = event_service.detail(database, quake.id)
    assert [report.id for report in detail.reports] == [earlier, later]
    assert (detail.event.report_count, detail.event.source_count) == (2, 2)
    overview = event_service.overview(database)
    assert ([each.id for each in overview.events], overview.total, overview.waiting) == ([quake.id, rows[rates].event_id], 2, 0)
    # Done once: nothing is queued again.
    assert event_service.enqueue_due(database) == []
    assert {job.status for job in jobs(database, KIND)} == {JobStatus.SUCCEEDED}


@pytest.fixture
def grey(database):
    """QUAKE and RATES with their events, and RESCUE in QUAKE's grey zone, not yet assigned."""
    [quake] = reported(database, "a", QUAKE, published_at=NOW - timedelta(hours=3))
    reported(database, "b", RATES, published_at=NOW - timedelta(hours=2))
    assign_all(database, Ollama(status=503))
    [rescue] = reported(database, "c", RESCUE)
    return quake, rescue


@pytest.mark.parametrize("same_as", [1, None], ids=["the same", "none of them"])
def test_in_the_grey_zone_the_llm_decides_among_the_events_to_choose_from(database, grey, same_as, caplog):
    quake, rescue = grey
    ollama = Ollama(json.dumps({"reason": "Rescue work after that quake.", "same_as": same_as}))

    with caplog.at_level(logging.INFO, logger=event_service.__name__):
        assign_all(database, ollama)

    [request] = ollama.requests
    # The rate decision is nothing alike: not offered.
    assert request["format"]["properties"]["same_as"] == {"enum": [1, None]}
    user = request["messages"][1]["content"]
    assert user.startswith(f"Report, published {NOW.date().isoformat()}:\nTitle: {RESCUE['title']}")
    assert f"Known events:\n1. {QUAKE['title']} (reported" in user and f"Latest report: {QUAKE['summary']}" in user
    rows = assigned(database)
    if same_as:
        assert (rows[rescue].event_id, rows[rescue].assignment_method) == (rows[quake].event_id, "llm")
        assert NEW_BELOW <= rows[rescue].assignment_score < ASSIGN_FROM
        assert event(database, rows[quake].event_id).last_reported_at == NOW
    else:
        assert rows[rescue].event_id != rows[quake].event_id
        assert (rows[rescue].assignment_method, rows[rescue].assignment_score) == ("new", None)
    # Logged with what the LLM was offered, so a new event after an offer shows as its "none of them".
    [logged] = [record for record in caplog.records if record.getMessage() == "Event assigned"]
    assert (logged.candidate_id, logged.method, logged.offered) == (rescue, "llm" if same_as else "new", 1)


def test_never_an_event_with_a_report_of_the_same_article(database):
    # The model told them apart within one article; alike as they read, they stay two events.
    first, second = reported(database, "a", QUAKE, SAME_QUAKE)

    assign_all(database, Ollama(status=503))

    rows = assigned(database)
    assert rows[first].event_id != rows[second].event_id
    assert {rows[first].assignment_method, rows[second].assignment_method} == {"new"}


def test_an_event_takes_reports_from_seven_days_before_its_first_to_seven_days_after_its_last(database):
    [founding] = reported(database, "a", QUAKE, published_at=NOW - timedelta(days=10))
    [too_late] = reported(database, "b", SAME_QUAKE)
    assign_all(database, Ollama(status=503))
    # Found later, by a backfill: reported before the event's first report.
    [earlier] = reported(database, "c", SAME_QUAKE, published_at=NOW - timedelta(days=12))

    assign_all(database, Ollama(status=503))

    rows = assigned(database)
    assert rows[too_late].event_id != rows[founding].event_id
    assert (rows[earlier].event_id, rows[earlier].assignment_method) == (rows[founding].event_id, "score")
    reach = event(database, rows[founding].event_id)
    assert (reach.first_reported_at, reach.last_reported_at) == (NOW - timedelta(days=12), NOW - timedelta(days=10))


def test_an_answer_naming_no_known_event_is_retried_with_the_job(database, grey):
    _quake, rescue = grey

    assign_all(database, Ollama(json.dumps({"reason": "x", "same_as": 4})))

    [job] = [job for job in jobs(database, KIND) if job.payload["candidate_id"] == rescue]
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 1)
    assert job.last_error == "UnusableChoice: same_as ausserhalb von 1 bis 1"
    assert assigned(database)[rescue].event_id is None


@pytest.mark.parametrize("away", [refused, Ollama(status=503)], ids=["no connection", "server failing"])
def test_a_question_that_finds_ollama_away_waits_without_using_an_attempt(database, grey, away):
    _quake, rescue = grey

    assign_all(database, away)

    [job] = [job for job in jobs(database, KIND) if job.payload["candidate_id"] == rescue]
    assert (job.status, job.attempts, job.last_error) == (JobStatus.QUEUED, 0, None)


def phase_one(database, candidate_id, ollama):
    with llm(ollama)() as client:
        return event_service.assign(database, candidate_id, client, LlmCallLimiter(database, threading.Event()))


def test_a_report_judged_new_joins_the_event_another_worker_founded_meanwhile(database):
    # Two workers at once: neither found an event to join; the second to assign finds the first one's.
    [first] = reported(database, "a", QUAKE)
    [second] = reported(database, "b", SAME_QUAKE)
    first_effect, second_effect = phase_one(database, first, Ollama(status=503)), phase_one(database, second, Ollama(status=503))

    with database.begin() as connection:
        first_effect(connection)
    with database.begin() as connection:
        second_effect(connection)

    rows = assigned(database)
    assert (rows[second].event_id, rows[second].assignment_method) == (rows[first].event_id, "score")


def test_an_llm_decision_the_events_to_choose_from_changed_under_is_made_again(database, grey):
    quake, rescue = grey
    effect = phase_one(database, rescue, Ollama(json.dumps({"reason": "Another incident.", "same_as": None})))
    # Meanwhile another worker founds an event the LLM was not shown, in the grey zone as well.
    [other] = reported(database, "d", {"title": "Rescuers search the rubble", "summary": "Rescue teams from Lhasa arrive.",
                                       "place": None, "actors": [], "event_type": "natural_disaster", "country": "CN"})
    with database.begin() as connection:
        phase_one(database, other, Ollama(status=503))(connection)

    with database.begin() as connection, pytest.raises(JobDeferred):
        effect(connection)

    assert assigned(database)[rescue].event_id is None
