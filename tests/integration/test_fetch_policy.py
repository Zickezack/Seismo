"""Fetch policy in PostgreSQL: request slots per host, the robots.txt cache and pauses (Retry-After)."""
import threading
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import text

from app.repositories import fetch_host_repository
from app.services.fetch_policy_service import PoliteHttp
from app.sources.errors import FetchDisallowed, HostBusy, SourceUnavailable
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


def test_a_permitted_request_skips_robots_txt_but_keeps_the_interval(database):
    requests = []

    def answer(request):
        requests.append(str(request.url))
        # api.reliefweb.int: every path disallowed, access granted by an approved appname instead.
        if request.url.path == "/robots.txt":
            return httpx2.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx2.Response(200, text="{}")

    http = HttpClient("", httpx2.MockTransport(answer))

    PoliteHttp(database, http, 10).get("https://api.example/v2/reports", "*/*", permitted=True)
    with pytest.raises(HostBusy):
        PoliteHttp(database, http, 10).get("https://api.example/v2/reports?page=2", "*/*", permitted=True)

    assert requests == ["https://api.example/v2/reports"]
    # Without the permission robots.txt still rules.
    with database.begin() as connection:
        connection.execute(text("UPDATE fetch_host SET next_slot_at = now(), last_request_at = NULL"))
    with pytest.raises(FetchDisallowed):
        PoliteHttp(database, http, 10).get("https://api.example/v2/reports", "*/*")


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


def paused_for(database, host="bank.example"):
    with database.connect() as connection:
        return connection.execute(text("SELECT paused_until - now() FROM fetch_host WHERE host = :host"), {"host": host}).scalar_one()


def test_a_reservation_during_a_pause_lies_after_it(database):
    with database.begin() as connection:
        fetch_host_repository.pause(connection, "bank.example", 120)

    assert timedelta(seconds=119) < reserve(database) <= timedelta(seconds=120)
    assert timedelta(seconds=129) < reserve(database) <= timedelta(seconds=130)


def test_a_shorter_pause_never_ends_a_longer_one(database):
    with database.begin() as connection:
        fetch_host_repository.pause(connection, "bank.example", 600)
        fetch_host_repository.pause(connection, "bank.example", 60)

    assert timedelta(seconds=599) < paused_for(database) <= timedelta(seconds=600)


@pytest.mark.parametrize(("retry_after", "pause"), [("120", timedelta(seconds=120)), ("999999", timedelta(hours=24))])
def test_retry_after_pauses_the_host_at_most_a_day(database, retry_after, pause):
    requests = []

    def answer(request):
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx2.Response(404)
        return httpx2.Response(429, headers={"retry-after": retry_after})

    http = HttpClient("", httpx2.MockTransport(answer))
    with pytest.raises(SourceUnavailable) as error:
        PoliteHttp(database, http, 10).get("https://bank.example/a", "*/*")
    assert error.value.retry_after == int(retry_after)
    assert pause - timedelta(seconds=1) < paused_for(database) <= pause

    # Nothing goes to the host during the pause - robots.txt neither, even once it is due again.
    with database.begin() as connection:
        connection.execute(text("UPDATE fetch_host SET robots_fetched_at = now() - interval '25 hours'"))
    with pytest.raises(HostBusy) as busy:
        PoliteHttp(database, http, 10, reserved_slot=datetime.now(UTC)).get("https://bank.example/b", "*/*")
    assert busy.value.slot - datetime.now(UTC) > pause - timedelta(seconds=2)
    assert requests == ["https://bank.example/robots.txt", "https://bank.example/a"]


def test_a_robots_txt_that_asks_for_a_pause_pauses_the_host(database):
    def answer(_request):
        return httpx2.Response(503, headers={"retry-after": "300"})

    with pytest.raises(SourceUnavailable, match=r"^robots.txt: HTTP 503 \(Retry-After 300 s\)$"):
        PoliteHttp(database, HttpClient("", httpx2.MockTransport(answer)), 10).get("https://bank.example/a", "*/*")

    assert timedelta(seconds=299) < paused_for(database) <= timedelta(seconds=300)


def host_row(database):
    with database.connect() as connection:
        return connection.execute(
            text("SELECT next_slot_at - now() AS next_slot, now() - last_request_at AS since_request FROM fetch_host")
        ).one()


def test_a_backfill_request_goes_while_the_host_is_free(database, web):
    requests, http = web

    PoliteHttp(database, http, 10, backfill=True).get("https://bank.example/a", "*/*")

    assert requests == ["https://bank.example/robots.txt", "https://bank.example/a"]
    row = host_row(database)
    assert timedelta(seconds=9) < row.next_slot <= timedelta(seconds=10) and row.since_request < timedelta(seconds=1)


def test_a_backfill_request_never_takes_a_slot_from_live_requests(database, web):
    requests, http = web
    PoliteHttp(database, http, 10).get("https://bank.example/a", "*/*")
    with pytest.raises(HostBusy):
        PoliteHttp(database, http, 10).get("https://bank.example/b", "*/*")  # live: reserves the slot in 10 s

    with pytest.raises(HostBusy) as busy:
        PoliteHttp(database, http, 10, backfill=True).get("https://bank.example/c", "*/*")

    # Free after the reserved live slot, and nothing reserved for it: the next live request gets that very slot.
    assert not busy.value.reserved
    assert timedelta(seconds=19) < busy.value.slot - datetime.now(UTC) <= timedelta(seconds=20)
    assert timedelta(seconds=19) < reserve(database) <= timedelta(seconds=20)
    assert "https://bank.example/c" not in requests


def test_a_backfill_request_keeps_the_interval_after_a_late_live_request(database, web):
    _requests, http = web
    with database.begin() as connection:
        # No slot ahead, but a live job that ran late sent its request just now.
        fetch_host_repository.save_robots(connection, "bank.example", "allow_all", None)
        connection.execute(text("UPDATE fetch_host SET next_slot_at = now() - interval '1 minute', last_request_at = now()"))

    with pytest.raises(HostBusy) as busy:
        PoliteHttp(database, http, 10, backfill=True).get("https://bank.example/a", "*/*")

    assert timedelta(seconds=9) < busy.value.slot - datetime.now(UTC) <= timedelta(seconds=10)


def test_a_backfill_request_waits_out_a_pause_without_reserving(database, web):
    requests, http = web
    with database.begin() as connection:
        fetch_host_repository.pause(connection, "bank.example", 120)

    with pytest.raises(HostBusy) as busy:
        PoliteHttp(database, http, 10, backfill=True).get("https://bank.example/a", "*/*")

    assert not busy.value.reserved
    assert timedelta(seconds=119) < busy.value.slot - datetime.now(UTC) <= timedelta(seconds=120)
    assert requests == []
    # A live request still gets the first slot after the pause.
    assert timedelta(seconds=119) < reserve(database) <= timedelta(seconds=120)
