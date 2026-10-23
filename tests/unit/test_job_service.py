"""Rules of the job queue without a database: the repository is replaced where needed."""
import uuid
from datetime import UTC, datetime

import pytest

from app.db.models import JobStatus
from app.jobs.handlers import build_handlers
from app.repositories import job_repository
from app.repositories.job_repository import ClaimedJob
from app.services import job_service
from app.services.job_service import BulkResult, JobListItem


def claimed(attempts, max_attempts=3):
    return ClaimedJob(id=1, kind="testjob", payload={}, attempts=attempts, max_attempts=max_attempts, lease_token=uuid.uuid4())


@pytest.fixture
def writes(monkeypatch):
    """Record the repository writes of record_failure; `lease_held` False simulates a takeover."""
    calls = []
    state = {"lease_held": True}

    def mark_failed(_connection, _job, error):
        calls.append(("failed", error))
        return state["lease_held"]

    def schedule_retry(_connection, _job, delay_seconds, error):
        calls.append(("retry", delay_seconds, error))
        return state["lease_held"]

    monkeypatch.setattr(job_repository, "mark_failed", mark_failed)
    monkeypatch.setattr(job_repository, "schedule_retry", schedule_retry)
    return calls, state


def test_retry_delay_doubles_from_thirty_seconds_up_to_an_hour():
    assert [job_service.retry_delay_seconds(attempt) for attempt in range(1, 9)] == [30, 60, 120, 240, 480, 960, 1920, 3600]
    assert job_service.retry_delay_seconds(50) == 3600


def test_full_text_attempts_span_about_a_day():
    waited = sum(job_service.retry_delay_seconds(attempt) for attempt in range(1, job_service.FETCH_ARTICLE_MAX_ATTEMPTS))

    assert 24 * 3600 <= waited < 25 * 3600


def test_failed_attempt_with_attempts_left_is_retried(writes):
    calls, _state = writes

    status = job_service.record_failure(None, claimed(attempts=2), TimeoutError("Ollama"), permanent=False)

    assert status == JobStatus.QUEUED
    assert calls == [("retry", 60, "TimeoutError: Ollama")]


@pytest.mark.parametrize(("attempts", "permanent"), [(3, False), (1, True)])
def test_last_attempt_or_permanent_error_fails_the_job(writes, attempts, permanent):
    calls, _state = writes

    status = job_service.record_failure(None, claimed(attempts), ValueError("ungültig"), permanent=permanent)

    assert status == JobStatus.FAILED
    assert calls == [("failed", "ValueError: ungültig")]


def test_failure_after_a_takeover_changes_nothing(writes):
    _calls, state = writes
    state["lease_held"] = False

    assert job_service.record_failure(None, claimed(attempts=1), RuntimeError(), permanent=False) is None


def test_error_description_names_the_type_and_is_bounded():
    assert job_service.describe_error(RuntimeError("kaputt")) == "RuntimeError: kaputt"
    assert job_service.describe_error(TimeoutError()) == "TimeoutError"
    assert len(job_service.describe_error(ValueError("x" * 5000))) == job_service.MAX_ERROR_LENGTH


@pytest.mark.parametrize(
    ("raw", "expected"), [("failed", JobStatus.FAILED), ("queued", JobStatus.QUEUED), ("", None), (None, None), ("FAILED", None), ("x' OR 1=1", None)]
)
def test_status_filter_accepts_known_statuses_only(raw, expected):
    assert job_service.parse_status(raw) == expected


def test_every_status_has_a_german_label():
    assert set(job_service.STATUS_LABELS) == set(JobStatus)


def test_every_job_kind_the_worker_runs_has_a_german_label():
    # A kind without a handler (e.g. removed in a later version) still shows its technical name.
    assert set(build_handlers(engine=None, backup_dir=None, limiter=None, stop_event=None)) <= set(job_service.KIND_LABELS)


@pytest.mark.parametrize(("kind", "options"), [("", {}), ("testjob", {"max_attempts": 0}), ("testjob", {"priority": 7})])
def test_enqueue_rejects_invalid_jobs(kind, options):
    with pytest.raises(ValueError):
        job_service.enqueue(None, kind, **options)


def test_requeue_without_selection_needs_no_database():
    assert job_service.requeue_failed(None, []) == BulkResult(processed=0, blocked=[])


def test_list_item_labels_and_requeue_only_for_failed_jobs():
    now = datetime.now(UTC)
    item = JobListItem(
        id=1, kind="unknown-kind", status=JobStatus.RUNNING, attempts=1, max_attempts=5, priority=0, created_at=now,
        updated_at=now, run_after=now, lease_expires_at=now, lease_owner="worker:1", last_error=None,
    )

    assert item.kind_label == "unknown-kind"
    assert item.status_label == "Läuft"
    assert not item.can_requeue
    assert JobListItem(**{**item.__dict__, "kind": "testjob", "status": JobStatus.FAILED}).can_requeue
    assert JobListItem(**{**item.__dict__, "kind": "testjob"}).kind_label == "Testjob"
    # A backfill's full text is the same kind, marked; the backfill job itself says so by its label.
    assert JobListItem(**{**item.__dict__, "kind": "fetch_article", "priority": 1}).kind_label == "Volltext abrufen (Backfill)"
    assert JobListItem(**{**item.__dict__, "kind": "backfill", "priority": 1}).kind_label == "Backfill abrufen"
