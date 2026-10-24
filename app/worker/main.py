"""Worker process: runs the jobs of the PostgreSQL job queue (app/jobs).

Every tick checks readiness and writes a heartbeat file that the Docker health check reads. While a job
runs, every lease renewal writes it as well, so a long job never makes the worker look dead. Once an
hour the worker deletes finished jobs whose retention period has passed; once a minute it queues a
backup job if a backup is due (app/services/backup_service.py), a fetch job for every source whose
next fetch is due (app/services/ingestion_service.py) and a normalization for every article whose full
text is settled (app/services/normalization_service.py).
"""
import logging
import os
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.migrations import head_revision
from app.db.session import create_db_engine
from app.jobs.handlers import build_handlers
from app.jobs.runner import Handler, run_next_job
from app.llm.limiter import LlmCallLimiter
from app.logging_config import configure_logging
from app.services import backup_service, ingestion_service, normalization_service
from app.services.health_service import HealthReport, check_readiness
from app.db.models import JobStatus
from app.services.job_service import LEASE_SECONDS, purge_finished_jobs

logger = logging.getLogger(__name__)

# Inside the container filesystem on purpose: the heartbeat only matters while this process runs.
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "seismo-worker-heartbeat"
# The retention periods are whole days; checking hourly keeps the job table small enough.
PURGE_INTERVAL_SECONDS = 60 * 60
# A backup starts at most this late; the check only lists the backup directory.
BACKUP_CHECK_INTERVAL_SECONDS = 60
# A source is fetched at most this late after its interval; the smallest interval is 5 minutes.
FETCH_CHECK_INTERVAL_SECONDS = 60
# An article is normalized at most this late after its full text is settled.
NORMALIZE_CHECK_INTERVAL_SECONDS = 60


def tick(engine: Engine, expected_revision: str, heartbeat_file: Path) -> HealthReport:
    report = check_readiness(engine, expected_revision)
    if report.ready:
        heartbeat_file.touch()
    return report


def run(settings: Settings, stop_event: threading.Event, heartbeat_file: Path = HEARTBEAT_FILE) -> None:
    engine = create_db_engine(settings)
    expected_revision = head_revision()
    handlers = build_handlers(
        engine, settings.backup_dir, LlmCallLimiter(engine, stop_event), stop_event, settings.fetch_contact
    )
    owner = worker_identity()
    # Several renewals per lease, and at least one heartbeat per poll interval (app/worker/healthcheck.py).
    renew_interval = min(settings.worker_poll_interval_seconds, LEASE_SECONDS / 3)
    logger.info(
        "Worker started",
        extra={"poll_interval_seconds": settings.worker_poll_interval_seconds, "job_kinds": sorted(handlers), "owner": owner},
    )
    was_ready = None
    next_purge = next_backup_check = next_fetch_check = next_normalize_check = time.monotonic()
    try:
        while not stop_event.is_set():
            report = tick(engine, expected_revision, heartbeat_file)
            # Log readiness changes only; the health service logs each failed check with its cause.
            if report.ready != was_ready:
                _log_readiness(report)
                was_ready = report.ready
            if report.ready and time.monotonic() >= next_purge:
                next_purge = time.monotonic() + PURGE_INTERVAL_SECONDS
                _purge_finished_jobs(engine)
            if report.ready and time.monotonic() >= next_backup_check:
                next_backup_check = time.monotonic() + BACKUP_CHECK_INTERVAL_SECONDS
                _enqueue_backup_if_due(engine, settings.backup_dir)
            if report.ready and time.monotonic() >= next_fetch_check:
                next_fetch_check = time.monotonic() + FETCH_CHECK_INTERVAL_SECONDS
                _enqueue_due_fetches(engine)
            if report.ready and time.monotonic() >= next_normalize_check:
                next_normalize_check = time.monotonic() + NORMALIZE_CHECK_INTERVAL_SECONDS
                _enqueue_due_normalizations(engine)
            # After a job, take the next one at once; wait only while the queue is empty.
            if report.ready and _run_next_job(engine, handlers, owner, renew_interval, heartbeat_file):
                continue
            stop_event.wait(settings.worker_poll_interval_seconds)
    finally:
        engine.dispose()
        heartbeat_file.unlink(missing_ok=True)
        logger.info("Worker stopped")


def _run_next_job(
    engine: Engine, handlers: Mapping[str, Handler], owner: str, renew_interval: float, heartbeat_file: Path
) -> bool:
    try:
        return run_next_job(engine, handlers, owner, renew_interval, on_renewed=heartbeat_file.touch)
    except SQLAlchemyError as error:
        # A job claimed before the error keeps its lease until it runs out; then another attempt takes over.
        logger.warning("Job queue unavailable: %s", getattr(error, "orig", None) or error)
        return False


def _purge_finished_jobs(engine: Engine) -> None:
    try:
        deleted = purge_finished_jobs(engine)
    except SQLAlchemyError as error:
        # Tried again at the next interval; finished jobs only take up space meanwhile.
        logger.warning("Purging finished jobs failed: %s", getattr(error, "orig", None) or error)
        return
    if any(deleted.values()):
        logger.info(
            "Finished jobs purged",
            extra={"succeeded": deleted[JobStatus.SUCCEEDED], "failed": deleted[JobStatus.FAILED]},
        )


def _enqueue_backup_if_due(engine: Engine, backup_dir: Path) -> None:
    try:
        job_id = backup_service.enqueue_if_due(engine, backup_dir)
    except (SQLAlchemyError, OSError) as error:
        # Checked again at the next interval.
        logger.warning("Backup check failed: %s", getattr(error, "orig", None) or error)
        return
    if job_id is not None:
        logger.info("Backup job queued", extra={"job_id": job_id})


def _enqueue_due_fetches(engine: Engine) -> None:
    try:
        job_ids = ingestion_service.enqueue_due_fetches(engine)
    except SQLAlchemyError as error:
        # Checked again at the next interval; a source is fetched a little late meanwhile.
        logger.warning("Fetch check failed: %s", getattr(error, "orig", None) or error)
        return
    if job_ids:
        logger.info("Fetch jobs queued", extra={"jobs": len(job_ids)})


def _enqueue_due_normalizations(engine: Engine) -> None:
    try:
        job_ids = normalization_service.enqueue_due(engine)
    except SQLAlchemyError as error:
        # Checked again at the next interval; the articles wait meanwhile.
        logger.warning("Normalization check failed: %s", getattr(error, "orig", None) or error)
        return
    if job_ids:
        logger.info("Normalization jobs queued", extra={"jobs": len(job_ids)})


def worker_identity() -> str:
    """Shown as the worker of a running job: container hostname and process id."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _log_readiness(report: HealthReport) -> None:
    if report.ready:
        logger.info("Worker ready")
    else:
        logger.warning(
            "Worker not ready",
            extra={"database_reachable": report.database_reachable, "schema_current": report.schema_current},
        )


def main() -> None:
    settings = Settings.from_env()
    configure_logging("worker", settings.log_level_number)
    stop_event = threading.Event()
    # Docker sends SIGTERM on stop; finish the current job instead of dying mid-work. A job still waiting
    # for an LLM slot goes back into the queue (app/jobs/errors.py, JobInterrupted).
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signal_number, lambda *_: stop_event.set())
    run(settings, stop_event)
