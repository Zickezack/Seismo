"""The job kinds the worker runs. A new kind: a handler here and its label in job_service.KIND_LABELS."""
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine

from app.jobs.errors import JobDeferred, JobInterrupted, PermanentJobError
from app.jobs.runner import Effect, Handler
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import LlmRejected, LlmUnreachable, OllamaClient
from app.repositories.backup_repository import ToolInterrupted
from app.repositories.job_repository import ClaimedJob
from app.db.models import BACKFILL_PRIORITY
from app.services import (
    backfill_service,
    backtest_service,
    backup_service,
    entity_service,
    event_service,
    extraction_service,
    history_service,
    impact_service,
    ingestion_service,
    normalization_service,
    price_service,
    situation_service,
    tracking_service,
)
from app.services.job_service import (
    ASSIGN_EVENT_JOB_KIND,
    ASSESS_IMPACTS_JOB_KIND,
    ASSIGN_SITUATION_JOB_KIND,
    BACKFILL_JOB_KIND,
    BACKFILL_MINUTES_JOB_KIND,
    BACKUP_JOB_KIND,
    EXTRACT_EVENTS_JOB_KIND,
    FETCH_ACTIONS_JOB_KIND,
    FETCH_ARTICLE_JOB_KIND,
    COLLECT_PRICES_JOB_KIND,
    FETCH_BARS_JOB_KIND,
    FETCH_MINUTES_JOB_KIND,
    FETCH_PRICES_JOB_KIND,
    FETCH_SOURCE_JOB_KIND,
    NORMALIZE_JOB_KIND,
    PROPOSE_SITUATION_JOB_KIND,
    RECOGNISE_ENTITIES_JOB_KIND,
    REEXTRACT_EVENTS_JOB_KIND,
    RERECOGNISE_ENTITIES_JOB_KIND,
    RUN_BACKTEST_JOB_KIND,
    SYNC_MARKET_JOB_KIND,
    TEST_JOB_KIND,
)
from app.sources.errors import HostBusy, SourceRejected

TEST_JOB_SECONDS = 5


# Makes the client for one LLM job; None while no Ollama is configured (OLLAMA_BASE_URL).
LlmFactory = Callable[[], OllamaClient] | None
# A job that lost Ollama comes back after this; the worker claims none while Ollama does not serve the model.
LLM_WAIT = timedelta(seconds=60)


def build_handlers(
    engine: Engine,
    backup_dir: Path,
    limiter: LlmCallLimiter,
    stop_event: threading.Event,
    fetch_contact: str = "",
    llm: LlmFactory = None,
) -> dict[str, Handler]:
    return {
        TEST_JOB_KIND: lambda _job: run_test_job(limiter, stop_event),
        BACKUP_JOB_KIND: lambda _job: run_backup_job(engine, backup_dir, stop_event),
        FETCH_SOURCE_JOB_KIND: lambda job: run_fetch_job(engine, job, fetch_contact),
        FETCH_ARTICLE_JOB_KIND: lambda job: run_full_text_job(engine, job, fetch_contact),
        BACKFILL_JOB_KIND: lambda job: run_backfill_job(engine, job, fetch_contact),
        FETCH_PRICES_JOB_KIND: lambda job: run_price_job(engine, job, fetch_contact),
        SYNC_MARKET_JOB_KIND: lambda job: run_market_job(
            job, lambda: history_service.sync_step(engine, job.payload["step"], fetch_contact)
        ),
        FETCH_ACTIONS_JOB_KIND: lambda job: run_market_job(
            job, lambda: history_service.fetch_actions(engine, job.payload, fetch_contact)
        ),
        FETCH_BARS_JOB_KIND: lambda job: run_market_job(job, lambda: history_service.fetch_bars(engine, job.payload, fetch_contact)),
        COLLECT_PRICES_JOB_KIND: lambda job: run_price_job(engine, job, fetch_contact, tracking_service.collect),
        BACKFILL_MINUTES_JOB_KIND: lambda job: run_market_job(job, lambda: tracking_service.backfill_minutes(engine, job.payload, fetch_contact)),
        FETCH_MINUTES_JOB_KIND: lambda job: run_market_job(job, lambda: tracking_service.fetch_minutes(engine, job.payload, fetch_contact, _reserved_slot(job))),
        # Reads the database only: no request, no LLM.
        RUN_BACKTEST_JOB_KIND: lambda job: backtest_service.run(engine, job.payload),
        # Reads only the raw text: no request, no slot.
        NORMALIZE_JOB_KIND: lambda job: normalization_service.normalize(engine, job.payload["article_id"]),
        EXTRACT_EVENTS_JOB_KIND: lambda job: run_extraction_job(engine, job, llm, limiter, stop_event),
        ASSIGN_EVENT_JOB_KIND: lambda job: run_llm_job(
            llm, lambda client: event_service.assign(engine, job.payload["candidate_id"], client, limiter)
        ),
        ASSIGN_SITUATION_JOB_KIND: lambda job: run_llm_job(
            llm, lambda client: situation_service.assign(engine, job.payload, client, limiter)
        ),
        PROPOSE_SITUATION_JOB_KIND: lambda job: run_llm_job(
            llm, lambda client: situation_service.propose(engine, job.payload, client, limiter)
        ),
        ASSESS_IMPACTS_JOB_KIND: lambda job: run_llm_job(
            llm, lambda client: impact_service.assess(engine, job.payload, client, limiter)
        ),
        RECOGNISE_ENTITIES_JOB_KIND: lambda job: run_llm_job(
            llm, lambda client: entity_service.recognise(engine, job.payload["extraction_id"], client, limiter)
        ),
        REEXTRACT_EVENTS_JOB_KIND: lambda job: run_llm_job(
            llm,
            lambda client: extraction_service.extract(
                engine, job.payload["article_id"], client, limiter, stop_event, job.payload["extraction_id"],
                job.payload.get("reprocessing_id"),
            ),
        ),
        RERECOGNISE_ENTITIES_JOB_KIND: lambda job: run_llm_job(
            llm,
            lambda client: entity_service.recognise(
                engine, job.payload["extraction_id"], client, limiter, rerun=True, reprocessing_id=job.payload.get("reprocessing_id")
            ),
        ),
    }


def run_extraction_job(
    engine: Engine, job: ClaimedJob, llm: LlmFactory, limiter: LlmCallLimiter, stop_event: threading.Event | None = None
) -> Effect | None:
    return run_llm_job(llm, lambda client: extraction_service.extract(engine, job.payload["article_id"], client, limiter, stop_event))


def run_llm_job(llm: LlmFactory, work: Callable[[OllamaClient], Effect | None]) -> Effect | None:
    """A request the server refuses fails at once. An unreachable server is waited for without counting the attempt
    (JobDeferred); a timeout or an unusable answer is retried with it."""
    if llm is None:
        raise PermanentJobError("Kein Ollama konfiguriert (OLLAMA_BASE_URL)")
    try:
        with llm() as client:
            return work(client)
    except LlmRejected as error:
        raise PermanentJobError(str(error)) from None
    except LlmUnreachable:
        raise JobDeferred(datetime.now(UTC) + LLM_WAIT) from None


def run_test_job(limiter: LlmCallLimiter, stop_event: threading.Event) -> None:
    """Checks queue, worker and LLM limit end to end: holds a slot for a few seconds, like a short LLM call."""
    with limiter.slot():
        if stop_event.wait(TEST_JOB_SECONDS):
            raise JobInterrupted("Worker stoppt während des Testjobs")


def run_backup_job(engine: Engine, backup_dir: Path, stop_event: threading.Event) -> None:
    """The backup file is the whole result; no effect to write. A retry after a lost lease only adds a file."""
    try:
        backup_service.create_backup(engine, backup_dir, stop_event)
    except ToolInterrupted:
        raise JobInterrupted("Worker stoppt während des Backups") from None


# Payload key of the request slot a deferred job reserved at its host (fetch_policy_service), with the attempt
# it belongs to: a retry after a failure queues up again instead of jumping ahead with an old slot.
HOST_SLOT = "host_slot"


def run_fetch_job(engine: Engine, job: ClaimedJob, fetch_contact: str) -> Effect | None:
    """A source that answers with nothing usable fails at once; one that cannot be reached is retried."""
    try:
        return ingestion_service.fetch_source(engine, job.payload["source_id"], fetch_contact, _reserved_slot(job))
    except SourceRejected as error:
        raise PermanentJobError(str(error)) from None
    except HostBusy as busy:
        raise _deferral(busy, job) from None


def run_full_text_job(engine: Engine, job: ClaimedJob, fetch_contact: str) -> Effect | None:
    final_attempt = job.attempts >= job.max_attempts
    backfill = job.priority == BACKFILL_PRIORITY
    try:
        return ingestion_service.fetch_full_text(
            engine, job.payload["article_id"], fetch_contact, _reserved_slot(job), final_attempt, backfill
        )
    except HostBusy as busy:
        raise _deferral(busy, job) from None


def run_backfill_job(engine: Engine, job: ClaimedJob, fetch_contact: str) -> Effect | None:
    """One page of a backfill; the effect queues the next one. A lasting error ends the backfill instead."""
    final_attempt = job.attempts >= job.max_attempts
    try:
        return backfill_service.fetch_page(engine, job.payload["backfill_id"], fetch_contact, final_attempt)
    except HostBusy as busy:
        raise _deferral(busy, job) from None


def run_price_job(engine: Engine, job: ClaimedJob, fetch_contact: str, fetch=None) -> Effect | None:
    """The latest prices of a batch of Wertpapiere - their Live-Kurs, or `fetch`: the collection of tracked ones; a
    source that answers with nothing usable fails at once."""
    try:
        # A job queued before there were several sources: EODHD's.
        source = job.payload.get("source", "eodhd")
        return (fetch or price_service.fetch)(engine, source, job.payload["instrument_ids"], fetch_contact, _reserved_slot(job))
    except SourceRejected as error:
        raise PermanentJobError(str(error)) from None
    except HostBusy as busy:
        raise _deferral(busy, job) from None


def run_market_job(job: ClaimedJob, work: Callable[[], Effect | None]) -> Effect | None:
    """A step of a chain of the historical market data; an answer with nothing usable fails it at once, and the worker
    starts the chain again later from where it came (history_service)."""
    try:
        return work()
    except SourceRejected as error:
        raise PermanentJobError(str(error)) from None
    except HostBusy as busy:
        raise _deferral(busy, job) from None


def _reserved_slot(job: ClaimedJob) -> datetime | None:
    reserved = job.payload.get(HOST_SLOT)
    if not reserved or reserved.get("attempt") != job.attempts:
        return None
    return datetime.fromisoformat(reserved["at"])


def _deferral(busy: HostBusy, job: ClaimedJob) -> JobDeferred:
    if not busy.reserved:
        # A backfill request: back when the host is free, with nothing reserved to come back to.
        return JobDeferred(busy.slot)
    # A deferral does not count the attempt, so the job comes back with the same attempt number.
    return JobDeferred(busy.slot, {HOST_SLOT: {"at": busy.slot.isoformat(), "attempt": job.attempts}})
