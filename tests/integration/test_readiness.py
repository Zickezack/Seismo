import threading
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import AuthSettings
from app.db.migrations import head_revision
from app.web.main import create_app
from app.worker import main as worker
from tests.integration.alembic_helpers import upgrade

# The readiness check is public; any valid admin login will do.
AUTH = AuthSettings(admin_username="admin", admin_password="integration-test", session_secret="s" * 32)


def get_ready(settings):
    with TestClient(create_app(settings, AUTH)) as client:
        return client.get("/health/ready")


def test_api_is_unavailable_until_migrations_ran(engine, test_settings):
    response = get_ready(test_settings)
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "ok", "schema": "outdated"}

    upgrade(engine)

    response = get_ready(test_settings)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "schema": "current"}


def test_api_is_unavailable_with_wrong_credentials(engine, test_settings):
    upgrade(engine)

    response = get_ready(replace(test_settings, postgres_password="wrong-password"))

    assert response.status_code == 503
    assert response.json()["database"] == "unreachable"
    assert "wrong-password" not in response.text


def test_worker_tick_writes_heartbeat_once_migrated(engine, tmp_path):
    heartbeat = tmp_path / "heartbeat"

    assert not worker.tick(engine, head_revision(), heartbeat).ready
    assert not heartbeat.exists()

    upgrade(engine)

    assert worker.tick(engine, head_revision(), heartbeat).ready
    assert heartbeat.exists()


def test_worker_loop_runs_until_stopped(engine, test_settings, tmp_path):
    upgrade(engine)
    heartbeat = tmp_path / "heartbeat"
    stop_event = threading.Event()
    settings = replace(test_settings, worker_poll_interval_seconds=0.05)
    thread = threading.Thread(target=worker.run, args=(settings, stop_event, heartbeat))

    thread.start()
    deadline = time.monotonic() + 10
    while not heartbeat.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    heartbeat_written = heartbeat.exists()
    stop_event.set()
    thread.join(timeout=10)

    assert heartbeat_written
    assert not thread.is_alive()
    assert not heartbeat.exists()
