"""Caps parallel LLM calls across all workers, so the GPU is not overloaded.

The limit is a Plattform-Einstellung, read on every request for a slot, so a change applies without a
restart. Lowering it takes effect as running calls finish.
"""
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.jobs.errors import JobInterrupted
from app.repositories import llm_slot_repository
from app.services import platform_settings_service

SLOT_WAIT_SECONDS = 1.0


class LlmCallLimiter:
    def __init__(self, engine: Engine, stop_event: threading.Event, wait_seconds: float = SLOT_WAIT_SECONDS):
        self.engine = engine
        self.stop_event = stop_event
        self.wait_seconds = wait_seconds

    @contextmanager
    def slot(self) -> Iterator[int]:
        """Hold a slot for the duration of one LLM call; waits while all are taken.

        Raises JobInterrupted when the worker stops while waiting.
        """
        with self.engine.connect() as connection:
            # Autocommit: the lock lives as long as the connection, without a transaction left open.
            connection.execution_options(isolation_level="AUTOCOMMIT")
            slot = self._acquire(connection)
            try:
                yield slot
            finally:
                _release(connection, slot)

    def _acquire(self, connection: Connection) -> int:
        while True:
            limit = platform_settings_service.load(connection).llm_max_parallel_calls
            for slot in range(limit):
                if llm_slot_repository.try_acquire(connection, slot):
                    return slot
            if self.stop_event.wait(self.wait_seconds):
                raise JobInterrupted("Worker stoppt, während er auf einen LLM-Platz wartet")


def _release(connection: Connection, slot: int) -> None:
    try:
        released = llm_slot_repository.release(connection, slot)
    except SQLAlchemyError:
        released = False
    if not released:
        # Discarding the connection drops the lock with it; it must never go back into the pool holding one.
        connection.invalidate()
