"""How an adapter reports a failed fetch. The message is shown to the admin as the job's error."""
from datetime import datetime


class SourceError(Exception):
    """A failed fetch. `summary` is the adapter's own words and is logged; `detail` may quote what the source
    sent (e.g. an API's error text) and is only shown to the admin, after the summary (datamodel.md, rule 13)."""

    def __init__(self, summary: str, detail: str | None = None) -> None:
        super().__init__(f"{summary}: {detail}" if detail else summary)
        self.summary = summary
        self.detail = detail


class SourceRejected(SourceError):
    """The source answered, but not with anything usable (e.g. HTTP 404, no feed); a retry would not help."""


class SourceUnavailable(SourceError):
    """The source could not be reached (timeout, connection error, HTTP 5xx); a later attempt may succeed.

    `retry_after`: the seconds the host asked to wait (Retry-After header), if it did.
    """

    def __init__(self, summary: str, detail: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(summary, detail)
        self.retry_after = retry_after


class FetchDisallowed(SourceRejected):
    """The host's robots.txt does not allow the request."""


class HostBusy(Exception):
    """The host's next request slot lies ahead; `slot` is reserved for this request (fetch_policy_service).

    A backfill request reserves nothing (`reserved` False): `slot` is only when the host is free next.
    """

    def __init__(self, slot: datetime, reserved: bool = True) -> None:
        super().__init__(f"next slot at {slot.isoformat()}")
        self.slot = slot
        self.reserved = reserved
