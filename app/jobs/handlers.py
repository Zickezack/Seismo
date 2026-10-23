"""The job kinds the worker runs. A new kind: a handler here and its label in job_service.KIND_LABELS."""
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine

from app.jobs.errors import JobDeferred, JobInterrupted, PermanentJobError
from app.jobs.runner import Effect, Handler
from app.llm.limiter import LlmCallLimiter
from app.repositories.backup_repository import ToolInterrupted
from app.repositories.job_repository import ClaimedJob
from app.db.models import BACKFILL_PRIORITY
from app.services import backfill_service, backup_service, ingestion_service
from app.services.job_service import (
    BACKFILL_JOB_KIND,
    BACKUP_JOB_KIND,
    FETCH_ARTICLE_JOB_KIND,
    FETCH_SOURCE_JOB_KIND,
    TEST_JOB_KIND,
)
from app.sources.errors import HostBusy, SourceRejected

TEST_JOB_SECONDS = 5


def build_handlers(
    engine: Engine, backup_dir: Path, limiter: LlmCallLimiter, stop_event: threading.Event, fetch_contact: str = ""
) -> dict[str, Handler]:
    return {
        TEST_JOB_KIND: lambda _job: run_test_job(limiter, stop_event),
        BACKUP_JOB_KIND: lambda _job: run_backup_job(engine, backup_dir, stop_event),
        FETCH_SOURCE_JOB_KIND: lambda job: run_fetch_job(engine, job, fetch_contact),
        FETCH_ARTICLE_JOB_KIND: lambda job: run_full_text_job(engine, job, fetch_contact),
        BACKFILL_JOB_KIND: lambda job: run_backfill_job(engine, job, fetch_contact),
    }


def run_test_job(limiter: LlmCallLimiter, stop_event: threading.Event) -> None:
    """Checks queue, worker and LLM limit end to end: holds a slot for a few seconds, like a short LLM call."""
    with limiter.slot():
        if stop_event.wait(TEST_JOB_SECONDS):
            raise JobInterrupted("Worker stoppt während des Testjobs")


def run_backup_job(engine: Engine, backup_dir: Path, stop_event: threading.Event) -> None:
    """The backup file is the whole result; no effect to write. A retry after a lost lease only adds a file."""
    try:
        backup_service.create_backup(engine, backup_dir, stop_event)
    except ToolInterrupted:
        raise JobInterrupted("Worker stoppt während des Backups") from None


# Payload key of the request slot a deferred job reserved at its host (fetch_policy_service), with the attempt
# it belongs to: a retry after a failure queues up again instead of jumping ahead with an old slot.
HOST_SLOT = "host_slot"


def run_fetch_job(engine: Engine, job: ClaimedJob, fetch_contact: str) -> Effect | None:
    """A source that answers with nothing usable fails at once; one that cannot be reached is retried."""
    try:
        return ingestion_service.fetch_source(engine, job.payload["source_id"], fetch_contact, _reserved_slot(job))
    except SourceRejected as error:
        raise PermanentJobError(str(error)) from None
    except HostBusy as busy:
        raise _deferral(busy, job) from None


def run_full_text_job(engine: Engine, job: ClaimedJob, fetch_contact: str) -> Effect | None:
    final_attempt = job.attempts >= job.max_attempts
    backfill = job.priority == BACKFILL_PRIORITY
    try:
        return ingestion_service.fetch_full_text(
            engine, job.payload["article_id"], fetch_contact, _reserved_slot(job), final_attempt, backfill
        )
    except HostBusy as busy:
        raise _deferral(busy, job) from None


def run_backfill_job(engine: Engine, job: ClaimedJob, fetch_contact: str) -> Effect | None:
    """One page of a backfill; the effect queues the next one. A lasting error ends the backfill instead."""
    final_attempt = job.attempts >= job.max_attempts
    try:
        return backfill_service.fetch_page(engine, job.payload["backfill_id"], fetch_contact, final_attempt)
    except HostBusy as busy:
        raise _deferral(busy, job) from None


def _reserved_slot(job: ClaimedJob) -> datetime | None:
    reserved = job.payload.get(HOST_SLOT)
    if not reserved or reserved.get("attempt") != job.attempts:
        return None
    return datetime.fromisoformat(reserved["at"])


def _deferral(busy: HostBusy, job: ClaimedJob) -> JobDeferred:
    if not busy.reserved:
        # A backfill request: back when the host is free, with nothing reserved to come back to.
        return JobDeferred(busy.slot)
    # A deferral does not count the attempt, so the job comes back with the same attempt number.
    return JobDeferred(busy.slot, {HOST_SLOT: {"at": busy.slot.isoformat(), "attempt": job.attempts}})
