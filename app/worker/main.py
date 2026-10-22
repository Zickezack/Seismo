"""Worker process.

Until the job queue exists (Projektfundament und Laufzeitumgebung, story 4), the worker only checks
readiness on every tick and writes a heartbeat file that the Docker health check reads.
"""
import logging
import signal
import tempfile
import threading
from pathlib import Path

from sqlalchemy import Engine

from app.config import Settings
from app.db.migrations import head_revision
from app.db.session import create_db_engine
from app.logging_config import configure_logging
from app.services.health_service import HealthReport, check_readiness

logger = logging.getLogger(__name__)

# Inside the container filesystem on purpose: the heartbeat only matters while this process runs.
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "finnewsint-worker-heartbeat"


def tick(engine: Engine, expected_revision: str, heartbeat_file: Path) -> HealthReport:
    report = check_readiness(engine, expected_revision)
    if report.ready:
        heartbeat_file.touch()
    return report


def run(settings: Settings, stop_event: threading.Event, heartbeat_file: Path = HEARTBEAT_FILE) -> None:
    engine = create_db_engine(settings)
    expected_revision = head_revision()
    logger.info("Worker started", extra={"poll_interval_seconds": settings.worker_poll_interval_seconds})
    was_ready = None
    try:
        while not stop_event.is_set():
            report = tick(engine, expected_revision, heartbeat_file)
            # Log readiness changes only; the health service logs each failed check with its cause.
            if report.ready != was_ready:
                _log_readiness(report)
                was_ready = report.ready
            stop_event.wait(settings.worker_poll_interval_seconds)
    finally:
        engine.dispose()
        heartbeat_file.unlink(missing_ok=True)
        logger.info("Worker stopped")


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
    # Docker sends SIGTERM on stop; finish the current tick instead of dying mid-work.
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signal_number, lambda *_: stop_event.set())
    run(settings, stop_event)
