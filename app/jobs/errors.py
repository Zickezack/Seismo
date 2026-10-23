"""Exceptions a job handler raises to tell the runner how to treat its job. Any other exception means: retry."""
from datetime import datetime


class PermanentJobError(Exception):
    """Retrying cannot help (e.g. the payload names a record that no longer exists): fail at once."""


class JobInterrupted(Exception):
    """The worker is stopping before the job did its work: back into the queue, the attempt does not count."""


class JobDeferred(Exception):
    """Not yet, e.g. the host's next request slot lies ahead: back into the queue until `run_after`.

    The attempt does not count. `payload` is merged into the job's payload, e.g. to remember a reserved slot.
    """

    def __init__(self, run_after: datetime, payload: dict | None = None) -> None:
        super().__init__(f"deferred until {run_after.isoformat()}")
        self.run_after = run_after
        self.payload = payload or {}
