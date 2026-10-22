import contextlib
import logging

import pytest
from sqlalchemy.exc import OperationalError

from app.repositories import schema_repository
from app.services.health_service import HealthReport, check_readiness

HEAD = "3f9c1d2a7b60"


class FakeEngine:
    def __init__(self, error=None):
        self.error = error

    def connect(self):
        if self.error:
            raise self.error
        return contextlib.nullcontext(object())


@pytest.fixture
def database_revision(monkeypatch):
    def set_revision(revision):
        monkeypatch.setattr(schema_repository, "current_revision", lambda _connection: revision)

    return set_revision


def test_ready_when_database_is_at_the_newest_migration(database_revision):
    database_revision(HEAD)

    report = check_readiness(FakeEngine(), HEAD)

    assert report == HealthReport(database_reachable=True, schema_current=True)
    assert report.ready


@pytest.mark.parametrize("revision", [None, "0000older0000"])
def test_not_ready_when_migrations_are_missing(database_revision, revision):
    database_revision(revision)

    report = check_readiness(FakeEngine(), HEAD)

    assert report == HealthReport(database_reachable=True, schema_current=False)
    assert not report.ready


def test_not_ready_and_logged_when_database_is_unreachable(caplog):
    error = OperationalError("SELECT 1", {}, Exception("connection refused"))

    with caplog.at_level(logging.WARNING):
        report = check_readiness(FakeEngine(error), HEAD)

    assert report == HealthReport(database_reachable=False, schema_current=False)
    assert "connection refused" in caplog.text
