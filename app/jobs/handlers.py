"""The job kinds the worker runs. A new kind: a handler here and its label in job_service.KIND_LABELS."""
import threading

from app.jobs.errors import JobInterrupted
from app.jobs.runner import Handler
from app.llm.limiter import LlmCallLimiter
from app.services.job_service import TEST_JOB_KIND

TEST_JOB_SECONDS = 5


def build_handlers(limiter: LlmCallLimiter, stop_event: threading.Event) -> dict[str, Handler]:
    return {TEST_JOB_KIND: lambda _job: run_test_job(limiter, stop_event)}


def run_test_job(limiter: LlmCallLimiter, stop_event: threading.Event) -> None:
    """Checks queue, worker and LLM limit end to end: holds a slot for a few seconds, like a short LLM call."""
    with limiter.slot():
        if stop_event.wait(TEST_JOB_SECONDS):
            raise JobInterrupted("Worker stoppt während des Testjobs")
