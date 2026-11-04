"""Worker process: runs the jobs of the PostgreSQL job queue (app/jobs).

Every tick checks readiness and writes a heartbeat file that the Docker health check reads. While a job
runs, every lease renewal writes it as well, so a long job never makes the worker look dead. Once an
hour the worker deletes finished jobs whose retention period has passed; once a minute it queues a
backup job if a backup is due (app/services/backup_service.py), a fetch job for every source whose
next fetch is due (app/services/ingestion_service.py), a normalization for every article whose full
text is settled (app/services/normalization_service.py) and, with an Ollama configured, an extraction for the
articles that pass its pre-filter, within the hour's budget (app/services/extraction_service.py), an assignment
to its event for every event candidate without one (app/services/event_service.py), a recognition of the entities
for every extraction without one (app/services/entity_service.py) and the next jobs of a running reprocessing,
within its own budget (app/services/reprocessing_service.py). Once a minute, too, the reports of replaced readings
leave their events once the new ones have theirs; once an hour it measures the storage, if not yet that day
(app/services/storage_service.py), and deletes the raw texts whose retention period is over
(app/services/raw_text_service.py). Once a minute, too, it queues the fetch of the latest prices of the Wertpapiere
with a Live-Kurs whose interval is over, from each source that can be asked (app/services/price_service.py), and the
collection of the tracked ones; once an hour it compresses their prices kept long enough at full resolution
(app/services/tracking_service.py); every ten
minutes it starts the chains of the historical market data that are due and not under way
(app/services/history_service.py). Once a minute, too, every active alert rule looks for what is new to it and sends
what its throttle lets go (app/services/alert_service.py). Once an hour it weighs the events due against the Lagen and
looks for new ones, queuing the LLM's questions while Ollama serves the model (app/services/situation_service.py).
"""
import logging
import os
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from functools import partial
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
from app.llm.ollama import OllamaClient
from app.services import (
    alert_service,
    backup_service,
    entity_service,
    event_service,
    extraction_service,
    ingestion_service,
    history_service,
    normalization_service,
    price_service,
    raw_text_service,
    reprocessing_service,
    situation_service,
    storage_service,
    tracking_service,
)
from app.services.health_service import HealthReport, check_readiness
from app.db.models import JobStatus
from app.services.job_service import LEASE_SECONDS, LLM_JOB_KINDS, purge_finished_jobs

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
# An article reaches the extraction at most this late after its normalization, a candidate its event and an extraction
# its entities this late after the extraction; the extraction's budget counts per hour.
LLM_CHECK_INTERVAL_SECONDS = 60
# A replaced reading's reports leave their events at most this late after the new reading has its own.
RETIRE_CHECK_INTERVAL_SECONDS = 60
# Measured once a UTC day; checking hourly measures soon after midnight.
STORAGE_CHECK_INTERVAL_SECONDS = 60 * 60
# Retention periods are whole days; a raw text stays at most this much longer.
RAW_TEXT_CHECK_INTERVAL_SECONDS = 60 * 60
# The latest prices: each at most a minute after its interval of the Plattform-Einstellungen.
PRICE_CHECK_INTERVAL_SECONDS = 60
# Full resolution is kept for days; an hour more costs nothing.
COMPRESS_CHECK_INTERVAL_SECONDS = 60 * 60
# The chains of the historical market data run for hours; a failed one starts again at most this late.
HISTORY_CHECK_INTERVAL_SECONDS = 10 * 60
# An alert goes out at most this late after its event's report was assigned or its entities recognised.
ALERT_CHECK_INTERVAL_SECONDS = 60
# Lagen develop over weeks; an hour's delay costs nothing, and the LLM's budget counts per hour.
SITUATION_CHECK_INTERVAL_SECONDS = 60 * 60


def tick(engine: Engine, expected_revision: str, heartbeat_file: Path) -> HealthReport:
    report = check_readiness(engine, expected_revision)
    if report.ready:
        heartbeat_file.touch()
    return report


def run(settings: Settings, stop_event: threading.Event, heartbeat_file: Path = HEARTBEAT_FILE) -> None:
    engine = create_db_engine(settings)
    expected_revision = head_revision()
    llm = None
    if settings.ollama_base_url:
        llm = partial(OllamaClient, settings.ollama_base_url, settings.ollama_model, settings.ollama_num_ctx)
    handlers = build_handlers(
        engine, settings.backup_dir, LlmCallLimiter(engine, stop_event), stop_event, settings.fetch_contact, llm
    )
    owner = worker_identity()
    # Several renewals per lease, and at least one heartbeat per poll interval (app/worker/healthcheck.py).
    renew_interval = min(settings.worker_poll_interval_seconds, LEASE_SECONDS / 3)
    logger.info(
        "Worker started",
        extra={
            "poll_interval_seconds": settings.worker_poll_interval_seconds, "job_kinds": sorted(handlers), "owner": owner,
            "llm_model": settings.ollama_model if llm else None,
        },
    )
    if llm is None:
        logger.warning("No Ollama configured (OLLAMA_BASE_URL): no event extraction is queued")
    was_ready = llm_served = None
    next_purge = next_backup_check = next_fetch_check = next_normalize_check = next_llm_check = time.monotonic()
    next_retire_check = next_storage_check = next_raw_text_check = next_price_check = time.monotonic()
    next_history_check = next_alert_check = next_situation_check = next_compress_check = time.monotonic()
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
            if report.ready and llm and time.monotonic() >= next_llm_check:
                next_llm_check = time.monotonic() + LLM_CHECK_INTERVAL_SECONDS
                llm_served = _enqueue_due_llm_jobs(engine, llm, llm_served)
            if report.ready and time.monotonic() >= next_retire_check:
                next_retire_check = time.monotonic() + RETIRE_CHECK_INTERVAL_SECONDS
                _retire_replaced(engine)
            if report.ready and time.monotonic() >= next_storage_check:
                next_storage_check = time.monotonic() + STORAGE_CHECK_INTERVAL_SECONDS
                _measure_storage(engine, settings.backup_dir)
            if report.ready and time.monotonic() >= next_raw_text_check:
                next_raw_text_check = time.monotonic() + RAW_TEXT_CHECK_INTERVAL_SECONDS
                _delete_due_raw_texts(engine)
            if report.ready and time.monotonic() >= next_price_check:
                next_price_check = time.monotonic() + PRICE_CHECK_INTERVAL_SECONDS
                _enqueue_due_price_fetches(engine)
            if report.ready and time.monotonic() >= next_compress_check:
                next_compress_check = time.monotonic() + COMPRESS_CHECK_INTERVAL_SECONDS
                _compress_prices(engine)
            if report.ready and time.monotonic() >= next_history_check:
                next_history_check = time.monotonic() + HISTORY_CHECK_INTERVAL_SECONDS
                _enqueue_due_market_data(engine)
            if report.ready and time.monotonic() >= next_situation_check:
                next_situation_check = time.monotonic() + SITUATION_CHECK_INTERVAL_SECONDS
                _sweep_situations(engine, llm_served is True)
            if report.ready and time.monotonic() >= next_alert_check:
                next_alert_check = time.monotonic() + ALERT_CHECK_INTERVAL_SECONDS
                _sweep_alerts(engine)
            # After a job, take the next one at once; wait only while the queue is empty.
            claimable = _claimable(handlers, llm_served)
            if report.ready and _run_next_job(engine, claimable, owner, renew_interval, heartbeat_file):
                continue
            stop_event.wait(settings.worker_poll_interval_seconds)
    finally:
        engine.dispose()
        heartbeat_file.unlink(missing_ok=True)
        logger.info("Worker stopped")


def _claimable(handlers: Mapping[str, Handler], llm_served: bool | None) -> Mapping[str, Handler]:
    """Every kind but the LLM jobs while Ollama does not serve the model: they wait in the queue, without an attempt
    (userstories.md, Strukturierte Ereignisextraktion, story 4). Unknown yet (None): all of them."""
    if llm_served is not False:
        return handlers
    return {kind: handler for kind, handler in handlers.items() if kind not in LLM_JOB_KINDS}


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


def _retire_replaced(engine: Engine) -> None:
    try:
        retired = reprocessing_service.retire_replaced(engine)
    except SQLAlchemyError as error:
        # Checked again at the next interval; the old and the new reading share their events meanwhile.
        logger.warning("Retiring replaced reports failed: %s", getattr(error, "orig", None) or error)
        return
    if retired:
        logger.info("Replaced readings retired", extra={"readings": retired})


def _measure_storage(engine: Engine, backup_dir: Path) -> None:
    try:
        storage_service.measure_if_due(engine, backup_dir)
    except SQLAlchemyError as error:
        # Checked again at the next interval; the day's measurement comes later.
        logger.warning("Storage measurement failed: %s", getattr(error, "orig", None) or error)


def _enqueue_due_price_fetches(engine: Engine) -> None:
    try:
        job_ids = price_service.enqueue_due(engine) + tracking_service.enqueue_due(engine)
    except SQLAlchemyError as error:
        # Checked again at the next interval; a minute's prices come later.
        logger.warning("Price fetch check failed: %s", getattr(error, "orig", None) or error)
        return
    if job_ids:
        logger.debug("Price fetch jobs queued", extra={"jobs": len(job_ids)})


def _compress_prices(engine: Engine) -> None:
    try:
        tracking_service.compress_due(engine)
    except SQLAlchemyError as error:
        # Tried again at the next interval; the points keep their full resolution a while longer.
        logger.warning("Price compression failed: %s", getattr(error, "orig", None) or error)


def _enqueue_due_market_data(engine: Engine) -> None:
    try:
        job_ids = history_service.enqueue_due(engine)
    except SQLAlchemyError as error:
        # Checked again at the next interval; the chains go on where they came.
        logger.warning("Market data check failed: %s", getattr(error, "orig", None) or error)
        return
    if job_ids:
        logger.info("Market data jobs queued", extra={"jobs": len(job_ids)})


def _sweep_situations(engine: Engine, llm_available: bool) -> None:
    try:
        situation_service.sweep(engine, llm_available)
    except SQLAlchemyError as error:
        # Tried again at the next interval; the events wait for their Lagen an hour longer.
        logger.warning("Situation sweep failed: %s", getattr(error, "orig", None) or error)


def _sweep_alerts(engine: Engine) -> None:
    try:
        alert_service.sweep(engine)
    except SQLAlchemyError as error:
        # Tried again at the next interval; nothing found is lost, it goes out a minute later.
        logger.warning("Alert sweep failed: %s", getattr(error, "orig", None) or error)


def _delete_due_raw_texts(engine: Engine) -> None:
    try:
        raw_text_service.delete_due(engine)
    except SQLAlchemyError as error:
        # Tried again at the next interval; the raw texts take their space a while longer.
        logger.warning("Deleting raw texts failed: %s", getattr(error, "orig", None) or error)


def _enqueue_due_llm_jobs(engine: Engine, llm: Callable[[], OllamaClient], was_served: bool | None) -> bool:
    """Queue extractions, assignments and recognitions while Ollama serves the model: queued before it is pulled, they
    would only fail. Returns whether it does; a change is logged, the state itself not every minute."""
    with llm() as client:
        served = client.serves_model()
    if served != was_served:
        fields = {"model": client.model}
        if served:
            logger.info("LLM model available", extra=fields)
        else:
            logger.warning("LLM model not available: Ollama unreachable or the model not pulled", extra=fields)
    if not served:
        return False
    sweeps = (
        (extraction_service.enqueue_due, "Extraction"),
        (event_service.enqueue_due, "Assignment"),
        (entity_service.enqueue_due, "Entity recognition"),
        (reprocessing_service.enqueue_due, "Reprocessing"),
    )
    for enqueue_due, name in sweeps:
        try:
            job_ids = enqueue_due(engine)
        except SQLAlchemyError as error:
            # Checked again at the next interval; the articles, candidates, extractions or reprocessing wait meanwhile.
            logger.warning("%s check failed: %s", name, getattr(error, "orig", None) or error)
            continue
        if job_ids:
            logger.info(f"{name} jobs queued", extra={"jobs": len(job_ids)})
    return True


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
