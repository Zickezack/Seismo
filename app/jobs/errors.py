"""Exceptions a job handler raises to tell the runner how to treat its job. Any other exception means: retry."""


class PermanentJobError(Exception):
    """Retrying cannot help (e.g. the payload names a record that no longer exists): fail at once."""


class JobInterrupted(Exception):
    """The worker is stopping before the job did its work: back into the queue, the attempt does not count."""
