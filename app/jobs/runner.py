"""Runs one claimed job: two-phase handler, lease renewal, fenced completion.

A handler does its slow work (e.g. an LLM call) outside any transaction and returns an effect: a
function that writes the result. The effect runs in the same transaction that marks the job done, and
only while the lease is still this worker's. So a job retried after a crash or a takeover writes its
result exactly once, and a worker that lost its lease writes nothing.
"""
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.jobs.errors import JobInterrupted, PermanentJobError
from app.repositories import job_repository
from app.repositories.job_repository import ClaimedJob
from app.services import job_service

logger = logging.getLogger(__name__)

Effect = Callable[[Connection], None]
Handler = Callable[[ClaimedJob], Effect | None]


class LeaseLost(Exception):
    """Another worker has taken the job over; this worker's result is discarded."""


def run_next_job(
    engine: Engine,
    handlers: Mapping[str, Handler],
    owner: str,
    renew_interval: float,
    on_renewed: Callable[[], None],
) -> bool:
    """Claim and run the next due job of a kind in `handlers`. False when there is none."""
    with engine.begin() as connection:
        job = job_service.claim_next(connection, handlers.keys(), owner)
    if job is None:
        return False
    with keep_lease(engine, job, renew_interval, on_renewed):
        execute(engine, handlers[job.kind], job)
    return True


def execute(engine: Engine, handler: Handler, job: ClaimedJob) -> None:
    fields = {"job_id": job.id, "kind": job.kind, "attempt": job.attempts}
    logger.info("Job started", extra=fields)
    started = time.perf_counter()
    try:
        effect = handler(job)
        _complete(engine, job, effect)
    except LeaseLost:
        logger.warning("Job lease lost, result discarded", extra=fields)
    except JobInterrupted:
        with engine.begin() as connection:
            job_repository.release(connection, job)
        logger.info("Job interrupted, back in the queue", extra=fields)
    except Exception as error:
        with engine.begin() as connection:
            status = job_service.record_failure(connection, job, error, permanent=isinstance(error, PermanentJobError))
        # The error type only: messages may quote article text, which logs never contain (datamodel.md, rule 13).
        logger.warning("Job failed", extra=fields | {"error_type": type(error).__name__, "status": status})
    else:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info("Job succeeded", extra=fields | {"duration_ms": duration_ms})


def _complete(engine: Engine, job: ClaimedJob, effect: Effect | None) -> None:
    with engine.begin() as connection:
        if not job_repository.lock_if_leased(connection, job):
            raise LeaseLost
        if effect is not None:
            effect(connection)
        job_repository.mark_succeeded(connection, job)


@contextmanager
def keep_lease(engine: Engine, job: ClaimedJob, interval: float, on_renewed: Callable[[], None]) -> Iterator[None]:
    """Renew the job's lease in the background while the handler runs; on_renewed after each renewal."""
    stop = threading.Event()
    thread = threading.Thread(
        target=_renew_until_stopped, args=(engine, job, interval, on_renewed, stop), name=f"lease-{job.id}", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def _renew_until_stopped(engine, job, interval, on_renewed, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            with engine.begin() as connection:
                renewed = job_repository.renew_lease(connection, job, job_service.LEASE_SECONDS)
        except SQLAlchemyError:
            # The next round tries again; the lease outlasts several missed renewals.
            logger.warning("Lease renewal failed", extra={"job_id": job.id})
            continue
        if not renewed:
            logger.warning("Job lease lost", extra={"job_id": job.id})
            return
        on_renewed()
