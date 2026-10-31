import logging
import re
import threading
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db.models import JobStatus
from app.llm.ollama import ANSWER_TIMEOUT_SECONDS
from app.services.health_service import HealthReport
from app.sources import http
from app.worker import main as worker

READY = HealthReport(database_reachable=True, schema_current=True)
UNREACHABLE = HealthReport(database_reachable=False, schema_current=False)
SETTINGS = Settings(postgres_user="u", postgres_password="p", postgres_db="d", worker_poll_interval_seconds=0.01)


@pytest.fixture
def readiness(monkeypatch):
    """Replace the readiness check with a fixed sequence of reports."""

    def use(*reports, stop_event=None):
        remaining = list(reports)

        def fake_check(_engine, _expected_revision):
            report = remaining.pop(0)
            if not remaining and stop_event:
                stop_event.set()
            return report

        monkeypatch.setattr(worker, "check_readiness", fake_check)

    return use


@pytest.fixture
def queue(monkeypatch):
    """Replace the job runner: each call returns the next result (True: a job ran) or raises it."""

    def use(*results):
        remaining = list(results)
        renewal_callbacks = []

        def fake_run_next_job(_engine, _handlers, _owner, _renew_interval, on_renewed):
            renewal_callbacks.append(on_renewed)
            result = remaining.pop(0) if remaining else False
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(worker, "run_next_job", fake_run_next_job)
        return renewal_callbacks

    return use


class CountingEvent(threading.Event):
    """A stop event that counts how often the worker waited for the next poll."""

    waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        return super().wait(timeout)


@pytest.fixture
def no_database(monkeypatch):
    class FakeEngine:
        disposed = False

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    engine.purges = 0
    engine.backup_checks = []
    engine.fetch_checks = 0
    engine.normalize_checks = 0

    def purge(_engine):
        engine.purges += 1
        return {JobStatus.SUCCEEDED: 0, JobStatus.FAILED: 0}

    def enqueue_backup_if_due(_engine, backup_dir):
        engine.backup_checks.append(backup_dir)

    def enqueue_due_fetches(_engine):
        engine.fetch_checks += 1
        return []

    def enqueue_due_normalizations(_engine):
        engine.normalize_checks += 1
        return []

    monkeypatch.setattr(worker, "create_db_engine", lambda _settings: engine)
    monkeypatch.setattr(worker, "purge_finished_jobs", purge)
    monkeypatch.setattr(worker.backup_service, "enqueue_if_due", enqueue_backup_if_due)
    monkeypatch.setattr(worker.ingestion_service, "enqueue_due_fetches", enqueue_due_fetches)
    monkeypatch.setattr(worker.normalization_service, "enqueue_due", enqueue_due_normalizations)
    monkeypatch.setattr(worker, "head_revision", lambda: "head")
    return engine


def test_tick_writes_heartbeat_when_ready(readiness, tmp_path):
    readiness(READY)
    heartbeat = tmp_path / "heartbeat"

    assert worker.tick(None, "head", heartbeat) == READY
    assert heartbeat.exists()


def test_tick_writes_no_heartbeat_when_not_ready(readiness, tmp_path):
    readiness(UNREACHABLE)
    heartbeat = tmp_path / "heartbeat"

    worker.tick(None, "head", heartbeat)

    assert not heartbeat.exists()


def test_run_logs_only_changes_in_readiness_and_cleans_up(readiness, queue, no_database, tmp_path, caplog):
    queue()
    stop_event = threading.Event()
    readiness(UNREACHABLE, UNREACHABLE, READY, READY, stop_event=stop_event)
    heartbeat = tmp_path / "heartbeat"

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        worker.run(SETTINGS, stop_event, heartbeat)

    messages = [record.getMessage() for record in caplog.records]
    # SETTINGS name no Ollama: said once, at the start.
    assert messages == [
        "Worker started", "No Ollama configured (OLLAMA_BASE_URL): no event extraction is queued", "Worker not ready",
        "Worker ready", "Worker stopped",
    ]
    assert not heartbeat.exists()
    assert no_database.disposed


def test_run_returns_immediately_when_already_stopped(readiness, no_database, tmp_path):
    readiness()
    stop_event = threading.Event()
    stop_event.set()

    worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert no_database.disposed


def test_run_takes_the_next_job_at_once_and_waits_only_when_the_queue_is_empty(readiness, queue, no_database, tmp_path):
    stop_event = CountingEvent()
    readiness(READY, READY, READY, stop_event=stop_event)
    renewal_callbacks = queue(True, True, False)
    heartbeat = tmp_path / "heartbeat"

    worker.run(SETTINGS, stop_event, heartbeat)

    assert len(renewal_callbacks) == 3
    assert stop_event.waits == 1
    # Lease renewals during a job keep the heartbeat fresh.
    renewal_callbacks[0]()
    assert heartbeat.exists()


def test_no_job_is_taken_while_not_ready(readiness, queue, no_database, tmp_path):
    stop_event = threading.Event()
    readiness(UNREACHABLE, UNREACHABLE, stop_event=stop_event)

    renewal_callbacks = queue(True)

    worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert renewal_callbacks == []


def test_job_queue_error_is_logged_and_the_worker_keeps_running(readiness, queue, no_database, tmp_path, caplog):
    stop_event = threading.Event()
    readiness(READY, READY, stop_event=stop_event)
    renewal_callbacks = queue(OperationalError("UPDATE job", {}, Exception("connection lost")), False)

    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert len(renewal_callbacks) == 2
    assert "Job queue unavailable: connection lost" in caplog.text


def test_finished_jobs_are_purged_once_ready_and_then_only_hourly(readiness, queue, no_database, tmp_path):
    stop_event = threading.Event()
    readiness(UNREACHABLE, READY, READY, READY, stop_event=stop_event)
    queue()

    worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert no_database.purges == 1


def test_failed_purge_is_logged_and_the_worker_keeps_running(readiness, queue, no_database, tmp_path, caplog, monkeypatch):
    def failing_purge(_engine):
        raise OperationalError("DELETE FROM job", {}, Exception("connection lost"))

    monkeypatch.setattr(worker, "purge_finished_jobs", failing_purge)
    stop_event = threading.Event()
    readiness(READY, READY, stop_event=stop_event)
    renewal_callbacks = queue()

    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert "Purging finished jobs failed: connection lost" in caplog.text
    assert len(renewal_callbacks) == 2


def test_backup_check_runs_once_ready_and_then_only_every_minute(readiness, queue, no_database, tmp_path):
    stop_event = threading.Event()
    readiness(UNREACHABLE, READY, READY, READY, stop_event=stop_event)
    queue()

    worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert no_database.backup_checks == [SETTINGS.backup_dir]


def test_failed_backup_check_is_logged_and_the_worker_keeps_running(
    readiness, queue, no_database, tmp_path, caplog, monkeypatch
):
    def failing_check(_engine, _backup_dir):
        raise PermissionError("[Errno 13] Permission denied: '/backups'")

    monkeypatch.setattr(worker.backup_service, "enqueue_if_due", failing_check)
    stop_event = threading.Event()
    readiness(READY, READY, stop_event=stop_event)
    renewal_callbacks = queue()

    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert "Backup check failed: [Errno 13] Permission denied: '/backups'" in caplog.text
    assert len(renewal_callbacks) == 2


def test_fetch_check_runs_once_ready_and_then_only_every_minute(readiness, queue, no_database, tmp_path):
    stop_event = threading.Event()
    readiness(UNREACHABLE, READY, READY, READY, stop_event=stop_event)
    queue()

    worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert no_database.fetch_checks == 1
    assert no_database.normalize_checks == 1


def test_failed_normalization_check_is_logged_and_the_worker_keeps_running(
    readiness, queue, no_database, tmp_path, caplog, monkeypatch
):
    def failing_check(_engine):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(worker.normalization_service, "enqueue_due", failing_check)
    stop_event = threading.Event()
    readiness(READY, READY, stop_event=stop_event)
    renewal_callbacks = queue()

    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert "Normalization check failed: connection lost" in caplog.text
    assert len(renewal_callbacks) == 2


def test_failed_fetch_check_is_logged_and_the_worker_keeps_running(
    readiness, queue, no_database, tmp_path, caplog, monkeypatch
):
    def failing_check(_engine):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(worker.ingestion_service, "enqueue_due_fetches", failing_check)
    stop_event = threading.Event()
    readiness(READY, READY, stop_event=stop_event)
    renewal_callbacks = queue()

    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert "Fetch check failed: connection lost" in caplog.text
    assert len(renewal_callbacks) == 2


class FakeLlm:
    model = "qwen3.5:9b"

    def __init__(self, served):
        self.served = served

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        pass

    def serves_model(self):
        return self.served


def test_llm_jobs_are_queued_only_while_ollama_serves_the_model_and_a_change_is_logged(monkeypatch, caplog):
    queued = []
    monkeypatch.setattr(worker.extraction_service, "enqueue_due", lambda engine: queued.append(("extract", engine)) or [7])
    monkeypatch.setattr(worker.event_service, "enqueue_due", lambda engine: queued.append(("assign", engine)) or [8, 9])
    monkeypatch.setattr(worker.entity_service, "enqueue_due", lambda engine: queued.append(("recognise", engine)) or [10])

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        missing = worker._enqueue_due_llm_jobs("engine", FakeLlm(served=False), None)
        still_missing = worker._enqueue_due_llm_jobs("engine", FakeLlm(served=False), missing)
        served = worker._enqueue_due_llm_jobs("engine", FakeLlm(served=True), still_missing)

    assert (missing, still_missing, served) == (False, False, True)
    assert queued == [("extract", "engine"), ("assign", "engine"), ("recognise", "engine")]
    assert [record.getMessage() for record in caplog.records] == [
        "LLM model not available: Ollama unreachable or the model not pulled", "LLM model available", "Extraction jobs queued",
        "Assignment jobs queued", "Entity recognition jobs queued",
    ]


def test_a_failing_extraction_check_does_not_keep_the_assignments_from_being_queued(monkeypatch, caplog):
    def failing(_engine):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    queued = []
    monkeypatch.setattr(worker.extraction_service, "enqueue_due", failing)
    monkeypatch.setattr(worker.event_service, "enqueue_due", lambda engine: queued.append(engine) or [])
    monkeypatch.setattr(worker.entity_service, "enqueue_due", lambda engine: queued.append(engine) or [])

    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        assert worker._enqueue_due_llm_jobs("engine", FakeLlm(served=True), True)

    assert queued == ["engine", "engine"] and "Extraction check failed: connection lost" in caplog.text


def test_the_worker_s_stop_grace_period_outlasts_its_longest_job():
    # Docker kills the worker after stop_grace_period; it must outlast one LLM call and one fetch, with their effect.
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
    # The worker's own lines: indented below "  worker:", up to the next service.
    worker = re.search(r"^  worker:\n((?:(?:    .*)?\n)*)", compose, re.MULTILINE).group(1)
    minutes = int(re.search(r"^    stop_grace_period: (\d+)m$", worker, re.MULTILINE).group(1))

    assert minutes * 60 >= ANSWER_TIMEOUT_SECONDS + 30
    assert minutes * 60 >= http.TOTAL_TIMEOUT_SECONDS + http.TIMEOUT_SECONDS + 30


def test_llm_jobs_are_not_claimed_while_ollama_does_not_serve_the_model():
    handlers = {"fetch_source": "fetch", "extract_events": "extract"}

    assert worker._claimable(handlers, None) == handlers
    assert worker._claimable(handlers, True) == handlers
    assert worker._claimable(handlers, False) == {"fetch_source": "fetch"}
