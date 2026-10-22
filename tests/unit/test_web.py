"""Web layer without a database: the readiness check is replaced, the engine never connects."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.health_service import HealthReport
from app.web import middleware
from app.web.main import create_app
from app.web.routes import health

SETTINGS = Settings(postgres_user="u", postgres_password="p", postgres_db="d")


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS)) as client:
        yield client


@pytest.fixture
def readiness(monkeypatch):
    def use(report):
        monkeypatch.setattr(health, "check_readiness", lambda _engine, _revision: report)

    return use


def test_live_answers_without_database(client):
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_database_and_schema_are_ok(client, readiness):
    readiness(HealthReport(database_reachable=True, schema_current=True))

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "schema": "current"}


def test_unavailable_when_schema_is_outdated(client, readiness):
    readiness(HealthReport(database_reachable=True, schema_current=False))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "ok", "schema": "outdated"}


def test_unavailable_with_unknown_schema_when_database_is_unreachable(client, readiness):
    readiness(HealthReport(database_reachable=False, schema_current=False))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable", "schema": "unknown"}


def test_every_request_gets_an_id_and_one_log_line(client, caplog):
    with caplog.at_level(logging.DEBUG, logger=middleware.__name__):
        response = client.get("/health/live?query=private")

    [record] = [record for record in caplog.records if record.name == middleware.__name__]
    assert response.headers[middleware.REQUEST_ID_HEADER] == record.request_id
    assert record.method == "GET"
    assert record.path == "/health/live"
    assert record.status == 200
    assert record.duration_ms >= 0
    assert "private" not in str(record.__dict__)


@pytest.mark.parametrize(
    ("path", "status", "level"),
    [
        ("/health/ready", 200, logging.DEBUG),
        ("/health/ready", 503, logging.WARNING),
        ("/events", 200, logging.INFO),
        ("/events", 404, logging.INFO),
        ("/events", 500, logging.WARNING),
    ],
)
def test_log_level_keeps_health_polls_quiet_but_failures_visible(path, status, level):
    assert middleware._level_for(path, status) == level
