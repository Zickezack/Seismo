"""Slot handling of the LLM limiter without a database: the slot repository is replaced."""
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.jobs.errors import JobInterrupted
from app.llm.limiter import LlmCallLimiter
from app.repositories import llm_slot_repository
from app.services import platform_settings_service
from app.services.platform_settings_service import PlatformSettings


class FakeConnection:
    def __init__(self):
        self.options = {}
        self.invalidated = False

    def execution_options(self, **options):
        self.options.update(options)
        return self

    def invalidate(self):
        self.invalidated = True


class FakeEngine:
    def __init__(self):
        self.connections = []

    @contextmanager
    def connect(self):
        connection = FakeConnection()
        self.connections.append(connection)
        yield connection


class FreeingEvent(threading.Event):
    """Instead of waiting, another worker finishes its call: the slots in `frees` become free."""

    def __init__(self, slots, frees):
        super().__init__()
        self.slots, self.frees, self.waits = slots, frees, 0

    def wait(self, timeout=None):
        self.waits += 1
        self.slots.taken -= self.frees
        return self.is_set()


@pytest.fixture
def slots(monkeypatch):
    """Slots taken by other workers, the configured limit, and what this worker released."""
    state = SimpleNamespace(taken=set(), limit=1, released=[], release_result=True)

    def try_acquire(_connection, slot):
        if slot in state.taken:
            return False
        state.taken.add(slot)
        return True

    def release(_connection, slot):
        state.released.append(slot)
        if isinstance(state.release_result, Exception):
            raise state.release_result
        return state.release_result

    monkeypatch.setattr(llm_slot_repository, "try_acquire", try_acquire)
    monkeypatch.setattr(llm_slot_repository, "release", release)
    monkeypatch.setattr(platform_settings_service, "load", lambda _connection: PlatformSettings(state.limit))
    return state


def test_takes_the_first_free_slot_within_the_limit(slots):
    slots.limit, slots.taken = 3, {0}
    engine = FakeEngine()

    with LlmCallLimiter(engine, threading.Event()).slot() as slot:
        assert slot == 1

    assert slots.released == [1]
    [connection] = engine.connections
    assert connection.options == {"isolation_level": "AUTOCOMMIT"}
    assert not connection.invalidated


def test_waits_while_all_slots_are_taken(slots):
    slots.taken = {0}
    stop_event = FreeingEvent(slots, frees={0})

    with LlmCallLimiter(FakeEngine(), stop_event).slot() as slot:
        assert slot == 0

    assert stop_event.waits == 1


def test_stop_while_waiting_interrupts_the_job(slots):
    slots.taken = {0}
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(JobInterrupted), LlmCallLimiter(FakeEngine(), stop_event).slot():
        pytest.fail("got a slot although all were taken")

    assert slots.released == []


def test_slot_is_released_when_the_call_fails(slots):
    with pytest.raises(RuntimeError), LlmCallLimiter(FakeEngine(), threading.Event()).slot():
        raise RuntimeError("Ollama nicht erreichbar")

    assert slots.released == [0]


@pytest.mark.parametrize("release_result", [False, OperationalError("SELECT", {}, Exception("connection lost"))])
def test_connection_is_discarded_when_the_release_fails(slots, release_result):
    slots.release_result = release_result
    engine = FakeEngine()

    with LlmCallLimiter(engine, threading.Event()).slot():
        pass

    # A pooled connection still holding the lock would block the slot for good.
    assert engine.connections[0].invalidated
