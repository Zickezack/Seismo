"""Rules of the job queue: leases, retries, and what the job list shows."""
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import datetime

from sqlalchemy import Connection, Engine, Row

from app.db.models import BACKFILL_PRIORITY, LIVE_PRIORITY, JobStatus
from app.repositories import job_repository, llm_slot_repository
from app.repositories.job_repository import ClaimedJob
from app.services import platform_settings_service
from app.services.bulk import BulkResult

DEFAULT_MAX_ATTEMPTS = 5
# The running worker renews its lease every few seconds (app/jobs/runner.py), so this is only how
# long the job of a vanished worker waits before another worker takes it over.
LEASE_SECONDS = 60
RETRY_BASE_DELAY_SECONDS = 30
RETRY_MAX_DELAY_SECONDS = 60 * 60
MAX_ERROR_LENGTH = 2000
LIST_LIMIT = 100
LEASE_EXPIRED_ERROR = "Lease abgelaufen: Worker beendet oder nicht erreichbar"

TEST_JOB_KIND = "testjob"
BACKUP_JOB_KIND = "backup"
# With the doubling retry delay about three hours of attempts, so a backup that keeps failing
# (app/services/backup_service.py) adds a failed job every few hours, not every few minutes.
BACKUP_MAX_ATTEMPTS = 10

STATUS_LABELS = {
    JobStatus.QUEUED: "Wartend",
    JobStatus.RUNNING: "Läuft",
    JobStatus.SUCCEEDED: "Erledigt",
    JobStatus.FAILED: "Fehlgeschlagen",
}
FETCH_SOURCE_JOB_KIND = "fetch_source"
FETCH_ARTICLE_JOB_KIND = "fetch_article"
# With the doubling retry delay about 24 hours of attempts, so a full text survives a host that is down for
# hours (userstories.md, Quellenadapter und Ingestion, story 4). A deferral to a request slot counts none.
FETCH_ARTICLE_MAX_ATTEMPTS = 31
BACKFILL_JOB_KIND = "backfill"
NORMALIZE_JOB_KIND = "normalize_article"
KIND_LABELS = {
    TEST_JOB_KIND: "Testjob",
    BACKUP_JOB_KIND: "Datenbank-Backup",
    FETCH_SOURCE_JOB_KIND: "Quelle abrufen",
    FETCH_ARTICLE_JOB_KIND: "Volltext abrufen",
    BACKFILL_JOB_KIND: "Backfill abrufen",
    NORMALIZE_JOB_KIND: "Artikel normalisieren",
}


@dataclass(frozen=True)
class JobListItem:
    id: int
    kind: str
    status: str
    attempts: int
    max_attempts: int
    priority: int
    created_at: datetime
    updated_at: datetime
    run_after: datetime
    lease_expires_at: datetime | None
    lease_owner: str | None
    last_error: str | None

    @property
    def kind_label(self) -> str:
        label = KIND_LABELS.get(self.kind, self.kind)
        # A full text found by a backfill is the same kind of job, behind live work.
        return f"{label} (Backfill)" if self.priority == BACKFILL_PRIORITY and self.kind != BACKFILL_JOB_KIND else label

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def can_requeue(self) -> bool:
        return self.status == JobStatus.FAILED


@dataclass(frozen=True)
class JobOverview:
    status: JobStatus | None  # the active filter
    counts: dict[JobStatus, int]
    jobs: list[JobListItem]  # the newest LIST_LIMIT jobs matching the filter
    llm_slots_in_use: int
    llm_max_parallel_calls: int

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def matching(self) -> int:
        return self.counts[self.status] if self.status else self.total


def enqueue(
    connection: Connection,
    kind: str,
    payload: dict | None = None,
    *,
    dedupe_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    priority: int = LIVE_PRIORITY,
) -> int | None:
    """Add a job, inside the transaction that makes it necessary - for a follow-up job, a job's effect.

    Returns the new job's id, or None when a job with the same dedupe key is already waiting or running.
    """
    if not kind:
        raise ValueError("kind must not be empty")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if priority not in (LIVE_PRIORITY, BACKFILL_PRIORITY):
        raise ValueError("priority must be LIVE_PRIORITY or BACKFILL_PRIORITY")
    return job_repository.insert_job(connection, kind, payload or {}, dedupe_key, max_attempts, priority)


def enqueue_test_job(engine: Engine) -> int:
    with engine.begin() as connection:
        return enqueue(connection, TEST_JOB_KIND)


def claim_next(connection: Connection, kinds: Iterable[str], owner: str) -> ClaimedJob | None:
    """The next due job of these kinds. Jobs of a vanished worker (crash, kill, restart) go back first."""
    job_repository.fail_expired_leases(connection, LEASE_EXPIRED_ERROR)
    job_repository.requeue_expired_leases(connection, LEASE_EXPIRED_ERROR)
    return job_repository.claim_next(connection, kinds, owner, LEASE_SECONDS)


def record_failure(connection: Connection, job: ClaimedJob, error: BaseException, permanent: bool) -> JobStatus | None:
    """Retry later with a growing delay, or give up after the last attempt or on a permanent error.

    Returns the job's new status, or None when another worker has taken the job over meanwhile.
    """
    message = describe_error(error)
    if permanent or job.attempts >= job.max_attempts:
        return JobStatus.FAILED if job_repository.mark_failed(connection, job, message) else None
    delay = retry_delay_seconds(job.attempts)
    return JobStatus.QUEUED if job_repository.schedule_retry(connection, job, delay, message) else None


def purge_finished_jobs(engine: Engine) -> dict[JobStatus, int]:
    """Delete finished jobs once their retention period (Plattform-Einstellungen) has passed.

    Waiting and running jobs are never touched; a requeued job is no longer finished.
    """
    with engine.begin() as connection:
        settings = platform_settings_service.load(connection)
        return {
            JobStatus.SUCCEEDED: job_repository.delete_finished(
                connection, JobStatus.SUCCEEDED, settings.succeeded_jobs_retention_days
            ),
            JobStatus.FAILED: job_repository.delete_finished(
                connection, JobStatus.FAILED, settings.failed_jobs_retention_days
            ),
        }


def retry_delay_seconds(attempt: int) -> int:
    """30 s after the first failed attempt, doubling with every further one, at most an hour."""
    return min(RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1), RETRY_MAX_DELAY_SECONDS)


def describe_error(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
    return text[:MAX_ERROR_LENGTH]


def parse_status(raw: str | None) -> JobStatus | None:
    """A status filter from the URL; anything unknown means no filter."""
    return JobStatus(raw) if raw in JobStatus else None


def overview(engine: Engine, status: str | None) -> JobOverview:
    status = parse_status(status)
    with engine.connect() as connection:
        counts = job_repository.count_by_status(connection)
        rows = job_repository.latest(connection, status, LIST_LIMIT)
        slots_in_use = llm_slot_repository.count_in_use(connection)
        settings = platform_settings_service.load(connection)
    return JobOverview(
        status=status,
        counts={each: counts.get(each, 0) for each in JobStatus},
        jobs=[_list_item(row) for row in rows],
        llm_slots_in_use=slots_in_use,
        llm_max_parallel_calls=settings.llm_max_parallel_calls,
    )


def requeue_failed(engine: Engine, job_ids: Iterable[int]) -> BulkResult:
    """Put failed jobs back into the queue with a fresh set of attempts.

    Atomic (requirements.md, "Destructive and Bulk Actions"): if any selected job is blocked, none is
    requeued, and the result names the blockers.
    """
    job_ids = sorted(set(job_ids))
    if not job_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        jobs = job_repository.lock_jobs(connection, job_ids)
        blocked = _requeue_blockers(connection, job_ids, jobs)
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        job_repository.requeue(connection, job_ids)
    return BulkResult(processed=len(job_ids), blocked=[])


def _requeue_blockers(connection: Connection, job_ids: list[int], jobs: Sequence[Row]) -> list[str]:
    found = {job.id: job for job in jobs}
    keys = [job.dedupe_key for job in jobs if job.dedupe_key]
    # A job with a dedupe key may wait or run only once (uq_job_dedupe_key_active).
    active_keys = job_repository.active_dedupe_keys(connection, keys) if keys else set()
    selected_keys = set()
    blocked = []
    for job_id in job_ids:
        job = found.get(job_id)
        if job is None:
            reason = "nicht gefunden"
        elif job.status != JobStatus.FAILED:
            reason = STATUS_LABELS[job.status]
        elif job.dedupe_key in active_keys:
            reason = "gleicher Job wartet oder läuft bereits"
        elif job.dedupe_key and job.dedupe_key in selected_keys:
            reason = "gleicher Job mehrfach ausgewählt"
        else:
            selected_keys.add(job.dedupe_key)
            continue
        blocked.append(f"Job #{job_id} ({reason})")
    return blocked


def _list_item(row: Row) -> JobListItem:
    return JobListItem(**{field.name: row._mapping[field.name] for field in fields(JobListItem)})
