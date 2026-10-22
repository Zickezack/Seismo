import logging
import threading

import pytest

from app.config import Settings
from app.services.health_service import HealthReport
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
def no_database(monkeypatch):
    class FakeEngine:
        disposed = False

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(worker, "create_db_engine", lambda _settings: engine)
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


def test_run_logs_only_changes_in_readiness_and_cleans_up(readiness, no_database, tmp_path, caplog):
    stop_event = threading.Event()
    readiness(UNREACHABLE, UNREACHABLE, READY, READY, stop_event=stop_event)
    heartbeat = tmp_path / "heartbeat"

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        worker.run(SETTINGS, stop_event, heartbeat)

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["Worker started", "Worker not ready", "Worker ready", "Worker stopped"]
    assert not heartbeat.exists()
    assert no_database.disposed


def test_run_returns_immediately_when_already_stopped(readiness, no_database, tmp_path):
    readiness()
    stop_event = threading.Event()
    stop_event.set()

    worker.run(SETTINGS, stop_event, tmp_path / "heartbeat")

    assert no_database.disposed
