"""The job kinds the worker runs. A new kind: a handler here and its label in job_service.KIND_LABELS."""
import threading
from pathlib import Path

from sqlalchemy import Engine

from app.jobs.errors import JobInterrupted, PermanentJobError
from app.jobs.runner import Effect, Handler
from app.llm.limiter import LlmCallLimiter
from app.repositories.backup_repository import ToolInterrupted
from app.repositories.job_repository import ClaimedJob
from app.services import backup_service, ingestion_service
from app.services.job_service import BACKUP_JOB_KIND, FETCH_SOURCE_JOB_KIND, TEST_JOB_KIND
from app.sources.errors import SourceRejected

TEST_JOB_SECONDS = 5


def build_handlers(
    engine: Engine, backup_dir: Path, limiter: LlmCallLimiter, stop_event: threading.Event, fetch_contact: str = ""
) -> dict[str, Handler]:
    return {
        TEST_JOB_KIND: lambda _job: run_test_job(limiter, stop_event),
        BACKUP_JOB_KIND: lambda _job: run_backup_job(engine, backup_dir, stop_event),
        FETCH_SOURCE_JOB_KIND: lambda job: run_fetch_job(engine, job, fetch_contact),
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


def run_fetch_job(engine: Engine, job: ClaimedJob, fetch_contact: str) -> Effect | None:
    """A source that answers with nothing usable fails at once; one that cannot be reached is retried."""
    try:
        return ingestion_service.fetch_source(engine, job.payload["source_id"], fetch_contact)
    except SourceRejected as error:
        raise PermanentJobError(str(error)) from None
