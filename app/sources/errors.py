"""How an adapter reports a failed fetch. The message is shown to the admin as the job's error."""


class SourceRejected(Exception):
    """The source answered, but not with anything usable (e.g. HTTP 404, no feed); a retry would not help."""


class SourceUnavailable(Exception):
    """The source could not be reached (timeout, connection error, HTTP 5xx); a later attempt may succeed."""
