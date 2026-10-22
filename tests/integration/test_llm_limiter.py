"""LLM limit across workers: PostgreSQL advisory locks, limit from the Plattform-Einstellungen."""
import threading
from contextlib import ExitStack

import pytest
from sqlalchemy import text

from app.jobs.errors import JobInterrupted
from app.llm.limiter import LlmCallLimiter
from app.repositories import llm_slot_repository
from app.services import platform_settings_service
from tests.integration.alembic_helpers import upgrade


@pytest.fixture
def database(engine):
    upgrade(engine)
    return engine


def limiter(engine, stop_event=None):
    return LlmCallLimiter(engine, stop_event or threading.Event(), wait_seconds=0.02)


def set_limit(engine, value):
    platform_settings_service.save(engine, {"llm_max_parallel_calls": str(value)})


def slots_in_use(engine):
    with engine.connect() as connection:
        return llm_slot_repository.count_in_use(connection)


def acquire_in_background(engine, stop_event=None):
    """A worker asking for a slot: holds it until `release` is set. Reports the slot or JobInterrupted."""
    result, acquired, release = {}, threading.Event(), threading.Event()

    def call():
        try:
            with limiter(engine, stop_event).slot() as slot:
                result["slot"] = slot
                acquired.set()
                release.wait(10)
        except JobInterrupted as error:
            result["error"] = error
            acquired.set()

    thread = threading.Thread(target=call)
    thread.start()
    return thread, result, acquired, release


def test_default_limit_is_one_call_at_a_time(database):
    with limiter(database).slot() as slot:
        assert slot == 0
        assert slots_in_use(database) == 1

    assert slots_in_use(database) == 0


def test_never_more_parallel_calls_than_the_limit(database):
    set_limit(database, 2)

    with ExitStack() as calls:
        assert calls.enter_context(limiter(database).slot()) == 0
        assert calls.enter_context(limiter(database).slot()) == 1
        thread, result, acquired, release = acquire_in_background(database)

        assert not acquired.wait(0.3), "a third call got a slot while two were taken"
        assert slots_in_use(database) == 2

    # The first two calls ended; the waiting one gets a slot.
    assert acquired.wait(5)
    assert result["slot"] in (0, 1)
    release.set()
    thread.join(5)
    assert slots_in_use(database) == 0


def test_raising_the_limit_applies_without_a_restart(database):
    with limiter(database).slot():
        thread, result, acquired, release = acquire_in_background(database)
        assert not acquired.wait(0.2)

        set_limit(database, 2)

        assert acquired.wait(5)
        assert result["slot"] == 1
        release.set()
        thread.join(5)


def test_waiting_for_a_slot_ends_when_the_worker_stops(database):
    stop_event = threading.Event()
    with limiter(database).slot():
        thread, result, acquired, _release = acquire_in_background(database, stop_event)
        assert not acquired.wait(0.2)

        stop_event.set()

        assert acquired.wait(5)
        assert isinstance(result["error"], JobInterrupted)
        thread.join(5)


def test_slot_is_released_when_the_call_fails(database):
    with pytest.raises(RuntimeError), limiter(database).slot():
        raise RuntimeError("Ollama nicht erreichbar")

    assert slots_in_use(database) == 0
    with limiter(database).slot() as slot:
        assert slot == 0


def test_slot_of_a_killed_worker_is_free_again(database):
    with database.connect() as killed_worker:
        assert llm_slot_repository.try_acquire(killed_worker, 0)
        pid = killed_worker.execute(text("SELECT pg_backend_pid()")).scalar_one()
        killed_worker.commit()
        assert slots_in_use(database) == 1

        # What PostgreSQL sees when a worker process dies: its connection ends.
        with database.connect() as admin:
            admin.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        killed_worker.invalidate()

    with limiter(database).slot() as slot:
        assert slot == 0
