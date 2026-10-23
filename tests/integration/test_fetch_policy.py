"""Fetch policy in PostgreSQL: request slots per host and the robots.txt cache."""
import threading
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import text

from app.repositories import fetch_host_repository
from app.services.fetch_policy_service import PoliteHttp
from app.sources.errors import HostBusy
from app.sources.http import HttpClient
from tests.integration.alembic_helpers import upgrade


@pytest.fixture
def database(engine):
    upgrade(engine)
    return engine


@pytest.fixture
def web():
    requests = []

    def answer(request):
        requests.append(str(request.url))
        return httpx2.Response(404) if request.url.path == "/robots.txt" else httpx2.Response(200, text="ok")

    return requests, HttpClient("", httpx2.MockTransport(answer))


def reserve(database, interval=10):
    with database.begin() as connection:
        slot, now = fetch_host_repository.reserve_slot(connection, "bank.example", interval)
    return slot - now


def test_each_reservation_gets_a_slot_of_its_own_one_interval_apart(database):
    waits = [reserve(database) for _ in range(3)]

    assert waits[0] <= timedelta(0)
    assert timedelta(seconds=9) < waits[1] <= timedelta(seconds=10)
    assert timedelta(seconds=19) < waits[2] <= timedelta(seconds=20)


def test_concurrent_reservations_never_share_a_slot(database):
    slots, barrier = [], threading.Barrier(5)

    def run():
        barrier.wait()
        with database.begin() as connection:
            slots.append(fetch_host_repository.reserve_slot(connection, "bank.example", 10)[0])

    threads = [threading.Thread(target=run) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ordered = sorted(slots)
    assert [later - earlier for earlier, later in zip(ordered, ordered[1:])] == [timedelta(seconds=10)] * 4


def test_second_request_to_a_host_is_busy_with_its_slot_reserved(database, web):
    requests, http = web

    PoliteHttp(database, http, 10).get("https://bank.example/a", "*/*")
    with pytest.raises(HostBusy) as busy:
        PoliteHttp(database, http, 10).get("https://bank.example/b", "*/*")

    assert requests == ["https://bank.example/robots.txt", "https://bank.example/a"]
    # The next request reserves the slot after the busy one's.
    assert timedelta(seconds=19) < reserve(database) <= timedelta(seconds=20)
    assert busy.value.slot is not None


def test_robots_txt_is_asked_once_a_day(database, web):
    requests, http = web

    for path in ("a", "b"):
        with database.begin() as connection:
            connection.execute(text("UPDATE fetch_host SET next_slot_at = now(), last_request_at = NULL"))
        PoliteHttp(database, http, 1).get(f"https://bank.example/{path}", "*/*")
    assert requests.count("https://bank.example/robots.txt") == 1

    with database.begin() as connection:
        connection.execute(
            text("UPDATE fetch_host SET robots_fetched_at = now() - interval '25 hours', next_slot_at = now(), last_request_at = NULL")
        )
    PoliteHttp(database, http, 1).get("https://bank.example/c", "*/*")

    assert requests.count("https://bank.example/robots.txt") == 2


def test_a_late_job_still_keeps_the_minimum_interval(database, web, monkeypatch):
    requests, http = web
    slept = []
    monkeypatch.setattr("app.services.fetch_policy_service.time.sleep", lambda seconds: slept.append(seconds) or _move_back(database))
    PoliteHttp(database, http, 10).get("https://bank.example/a", "*/*")

    # A job deferred to a slot that the worker reached late, right after the previous request.
    PoliteHttp(database, http, 10, reserved_slot=datetime.now(UTC)).get("https://bank.example/b", "*/*")

    assert len(slept) == 1 and 9 < slept[0] <= 10
    assert requests[-1] == "https://bank.example/b"


def _move_back(database):
    # Instead of really waiting: the previous request now lies an interval back.
    with database.begin() as connection:
        connection.execute(text("UPDATE fetch_host SET last_request_at = now() - interval '10 seconds'"))
