"""The status page in PostgreSQL (userstories.md, Skalierung und Betriebsqualität, story 1): every LLM call of a job
recorded, the job kinds' hours stored once and caught up, and the page's figures from what the tables keep."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from app.db.models import Article, EventCandidate, Extraction, Job, JobHour, LlmCall
from app.services import platform_settings_service, status_service
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_impacts import answer, assess, hypothesis, oil  # noqa: F401
from tests.integration.test_forecasts import SAYS

NOW = datetime.now(UTC).replace(microsecond=0)
HOUR = status_service._hour(NOW) - timedelta(hours=1)  # the hour before the current one


def calls(database):
    with database.connect() as connection:
        return connection.execute(select(LlmCall).order_by(LlmCall.id)).all()


def test_every_llm_call_of_a_job_is_recorded_with_its_tokens_and_how_it_ended(database, oil):
    before = len(calls(database))
    assess(database, oil["event_id"], answer(hypothesis("Shell") | SAYS))

    [call] = calls(database)[before:]
    assert (call.kind, call.model, call.outcome, call.article_id) == ("assess_impacts", "qwen3.5:9b", "answered", None)
    assert call.job_id is not None and call.duration_ms >= 0 and call.called_at <= datetime.now(UTC)

    # An article's steps name it through their payloads.
    with database.connect() as connection:
        candidate = connection.execute(select(EventCandidate).where(EventCandidate.id == oil["candidate"])).one()
        assert status_service.article_of(connection, "assign_event", {"candidate_id": candidate.id}) == candidate.article_id
        assert status_service.article_of(connection, "recognise_entities", {"extraction_id": candidate.extraction_id}) == candidate.article_id


def job(connection, kind, status, attempts, run_after, started_at=None, finished_at=None, priority=0):
    values = {"kind": kind, "status": status, "attempts": attempts, "run_after": run_after, "started_at": started_at,
              "finished_at": finished_at, "priority": priority}
    connection.execute(insert(Job).values(values))


@pytest.fixture
def jobs(database):
    """In the hour before the current one: a normalization done at its second attempt, one failed after three; a
    backup still due since two hours."""
    with database.begin() as connection:
        job(connection, "normalize_article", "succeeded", 2, HOUR + timedelta(minutes=5), HOUR + timedelta(minutes=10), HOUR + timedelta(minutes=12))
        job(connection, "normalize_article", "failed", 3, HOUR + timedelta(minutes=10), HOUR + timedelta(minutes=40), HOUR + timedelta(minutes=41))
        job(connection, "backup", "queued", 0, NOW - timedelta(hours=2))
    return database


def hours(database):
    with database.connect() as connection:
        return {(row.hour, row.kind): row for row in connection.execute(select(JobHour))}


def test_the_hour_over_is_stored_once_per_kind_with_the_queue_then(jobs):
    assert status_service.measure_hours(jobs, NOW) == 2
    stored = hours(jobs)
    normalized = stored[(HOUR, "normalize_article")]
    assert (normalized.finished, normalized.failed, normalized.attempts, normalized.failed_attempts) == (2, 1, 5, 4)
    assert (normalized.started, normalized.wait_seconds, normalized.wait_seconds_max) == (2, 35 * 60, 30 * 60)
    assert (normalized.run_seconds, normalized.run_seconds_max) == (3 * 60, 2 * 60)
    backup = stored[(HOUR, "backup")]
    assert (backup.finished, backup.queued_due, backup.oldest_due_seconds) == (0, 1, pytest.approx(7200))
    assert status_service.measure_hours(jobs, NOW) == 0  # once

    # A worker away for three hours fills them in afterwards.
    assert status_service.measure_hours(jobs, NOW + timedelta(hours=3)) == 3
    assert sorted(hour for hour, kind in hours(jobs) if kind == "backup")[-1] == HOUR + timedelta(hours=3)


def test_the_page_measures_throughput_cost_latency_failures_and_bottlenecks(jobs, oil):
    platform_settings_service.save(jobs, {"llm_cost_rappen_per_hour": "360"})
    status_service.measure_hours(jobs, NOW)
    with jobs.begin() as connection:
        candidate = connection.execute(select(EventCandidate).where(EventCandidate.id == oil["candidate"])).one()
        article_id = candidate.article_id
        connection.execute(update(Article).where(Article.id == article_id).values(
            fetched_at=NOW - timedelta(minutes=50), normalized_at=NOW - timedelta(minutes=40),
        ))
        connection.execute(update(Extraction).where(Extraction.id == candidate.extraction_id).values(extracted_at=NOW - timedelta(minutes=20)))
        connection.execute(update(EventCandidate).where(EventCandidate.id == candidate.id).values(assigned_at=NOW - timedelta(minutes=15)))
        connection.execute(insert(LlmCall), [
            {"called_at": NOW - timedelta(minutes=20), "duration_ms": 50_000, "kind": "extract_events", "job_id": 1, "article_id": article_id,
             "model": "qwen3.5:9b", "prompt_tokens": 3000, "answer_tokens": 900, "outcome": "answered"},
            {"called_at": NOW - timedelta(minutes=18), "duration_ms": 10_000, "kind": "assess_impacts", "job_id": 2, "article_id": None,
             "model": "qwen3.5:9b", "prompt_tokens": 1000, "answer_tokens": 100, "outcome": "cut"},
            # Before the period: not counted.
            {"called_at": NOW - timedelta(days=2), "duration_ms": 99_000, "kind": "extract_events", "job_id": 3, "article_id": None,
             "model": "qwen3.5:9b", "prompt_tokens": 1, "answer_tokens": 1, "outcome": "answered"},
        ])

    status = status_service.overview(jobs, "day", NOW)

    assert (status.articles.live, status.extracted) == (1, 1)
    assert status.per_hour == pytest.approx(1 / 24)
    assert (status.llm_calls, status.llm_seconds, status.prompt_tokens, status.answer_tokens) == (2, 60.0, 4000, 1000)
    assert status.llm_busy == pytest.approx(60 / 86400)
    # Only the article's steps count per article: 50 s and 3900 tokens; at 3.60 a LLM hour 5 Rappen.
    assert (status.per_article_seconds, status.per_article_tokens) == (50.0, 3900)
    assert status.cost_per_article_label == "CHF 0.0500"
    steps = {each.kind: each for each in status.llm_steps}
    assert steps["assess_impacts"].per_article_seconds is None and steps["assess_impacts"].unanswered == 1
    latencies = {each.key: each for each in status.latencies}
    assert (latencies["normalized"].median, latencies["extracted"].median, latencies["assigned"].median) == (600, 1200, 300)
    assert latencies["total"].p90 == 35 * 60
    kinds = {each.kind: each for each in status.kinds}
    assert (kinds["normalize_article"].failed_attempts, kinds["normalize_article"].attempts) == (4, 5)
    assert status.failed_attempts >= 4
    [source] = status.sources
    assert (source.articles, source.with_event, source.llm_label) == (1, 1, "50 s")
    assert any(each.startswith("Warteschlange: der älteste fällige Job «Datenbank-Backup» wartet seit 2,0 h") for each in status.bottlenecks)
    assert len(status.history) == 25 and status.history[0].start == status_service._hour(NOW)
    assert sum(each.llm_calls for each in status.history) == 2

    week = status_service.overview(jobs, "week", NOW)
    assert week.history_unit == "day" and week.llm_calls == 3
    assert status_service.overview(jobs, "unknown", NOW).period == "day"
