"""Docker health check for the worker: healthy while the heartbeat file is fresh."""
import sys
import time
from pathlib import Path

from app.config import Settings
from app.worker.main import HEARTBEAT_FILE

# A few slow ticks are tolerated before the worker counts as unhealthy.
MISSED_TICKS_ALLOWED = 3


def is_heartbeat_fresh(heartbeat_file: Path, max_age_seconds: float, now: float | None = None) -> bool:
    try:
        modified = heartbeat_file.stat().st_mtime
    except FileNotFoundError:
        return False
    now = time.time() if now is None else now
    return now - modified <= max_age_seconds


def main() -> int:
    settings = Settings.from_env()
    max_age_seconds = settings.worker_poll_interval_seconds * MISSED_TICKS_ALLOWED
    return 0 if is_heartbeat_fresh(HEARTBEAT_FILE, max_age_seconds) else 1


if __name__ == "__main__":
    sys.exit(main())
