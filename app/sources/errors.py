"""How an adapter reports a failed fetch. The message is shown to the admin as the job's error."""
from datetime import datetime


class SourceRejected(Exception):
    """The source answered, but not with anything usable (e.g. HTTP 404, no feed); a retry would not help."""


class SourceUnavailable(Exception):
    """The source could not be reached (timeout, connection error, HTTP 5xx); a later attempt may succeed."""


class FetchDisallowed(SourceRejected):
    """The host's robots.txt does not allow the request."""


class HostBusy(Exception):
    """The host's next request slot lies ahead; `slot` is reserved for this request (fetch_policy_service)."""

    def __init__(self, slot: datetime) -> None:
        super().__init__(f"next slot at {slot.isoformat()}")
        self.slot = slot
