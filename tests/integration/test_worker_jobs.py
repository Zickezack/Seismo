"""The worker process runs queued jobs, and a restart neither loses a job nor leaves it stuck."""
import threading
import time
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.db.models import Job, JobStatus
from app.jobs import handlers
from app.llm.limiter import LlmCallLimiter
from app.services import job_service
from app.worker import main as worker
from tests.integration.alembic_helpers import upgrade


@pytest.fixture
def database(engine, monkeypatch):
    upgrade(engine)
    monkeypatch.setattr(handlers, "TEST_JOB_SECONDS", 0.2)
    return engine


@pytest.fixture
def start_worker(test_settings, tmp_path):
    """Start a worker process loop in a thread; returns its stop event, thread and heartbeat file."""
    started = []

    def start():
        stop_event = threading.Event()
        heartbeat = tmp_path / f"heartbeat-{len(started)}"
        settings = replace(test_settings, worker_poll_interval_seconds=0.05)
        thread = threading.Thread(target=worker.run, args=(settings, stop_event, heartbeat))
        thread.start()
        started.append((stop_event, thread))
        return stop_event, thread, heartbeat

    yield start
    for stop_event, thread in started:
        stop_event.set()
        thread.join(10)


def job(engine, job_id):
    with engine.connect() as connection:
        return connection.execute(select(Job.__table__).where(Job.id == job_id)).one()


def wait_for(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_worker_runs_a_test_job_to_the_end(database, start_worker):
    job_id = job_service.enqueue_test_job(database)

    start_worker()

    assert wait_for(lambda: job(database, job_id).status == JobStatus.SUCCEEDED)
    assert job(database, job_id).attempts == 1


def test_heartbeat_stays_fresh_during_a_long_job(database, start_worker, monkeypatch):
    monkeypatch.setattr(handlers, "TEST_JOB_SECONDS", 2)
    job_id = job_service.enqueue_test_job(database)
    _stop_event, _thread, heartbeat = start_worker()
    assert wait_for(lambda: job(database, job_id).status == JobStatus.RUNNING)

    heartbeat.unlink(missing_ok=True)

    # Only the lease renewal writes it now: the main loop is busy with the job.
    assert wait_for(heartbeat.exists, timeout=1)
    assert job(database, job_id).status == JobStatus.RUNNING


def test_stopped_worker_hands_a_waiting_job_back_and_the_restarted_one_runs_it(database, start_worker):
    job_id = job_service.enqueue_test_job(database)

    # Another worker's LLM call holds the only slot, so the test job waits for it.
    with LlmCallLimiter(database, threading.Event()).slot():
        stop_event, thread, _heartbeat = start_worker()
        assert wait_for(lambda: job(database, job_id).status == JobStatus.RUNNING)

        stop_event.set()
        thread.join(10)

        handed_back = job(database, job_id)
        assert (handed_back.status, handed_back.attempts, handed_back.lease_token) == (JobStatus.QUEUED, 0, None)

    start_worker()

    assert wait_for(lambda: job(database, job_id).status == JobStatus.SUCCEEDED)
    assert job(database, job_id).attempts == 1
