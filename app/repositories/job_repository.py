"""Data access for the job queue. Times always come from the database clock, never from a worker."""
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Connection, Row, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import ACTIVE_JOB_STATUSES, Job, JobStatus


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict
    attempts: int
    max_attempts: int
    lease_token: uuid.UUID


_CLEARED_LEASE = {"lease_token": None, "lease_owner": None, "lease_expires_at": None}
# The predicate of the partial index uq_job_dedupe_key_active, with its values written into the SQL. As
# bound parameters they break ON CONFLICT from the 11th insert on one connection: psycopg prepares the
# statement after 5 runs, PostgreSQL plans it generically after 5 more, and a generic plan cannot match
# parameters against the index predicate ("no unique or exclusion constraint matching").
_ACTIVE_DEDUPE_PREDICATE = Job.status.in_([literal(status.value, literal_execute=True) for status in ACTIVE_JOB_STATUSES])


def insert_job(connection: Connection, kind: str, payload: dict, dedupe_key: str | None, max_attempts: int) -> int | None:
    """The new job's id, or None when a job with this dedupe key is already waiting or running."""
    statement = (
        insert(Job)
        .values(kind=kind, payload=payload, dedupe_key=dedupe_key, max_attempts=max_attempts)
        .on_conflict_do_nothing(index_elements=[Job.dedupe_key], index_where=_ACTIVE_DEDUPE_PREDICATE)
        .returning(Job.id)
    )
    return connection.execute(statement).scalar_one_or_none()


def fail_expired_leases(connection: Connection, error: str) -> int:
    """Running jobs whose lease ran out on their last attempt."""
    statement = (
        update(Job)
        .where(*_lease_expired(), Job.attempts >= Job.max_attempts)
        .values(status=JobStatus.FAILED, last_error=error, finished_at=func.now(), updated_at=func.now(), **_CLEARED_LEASE)
    )
    return connection.execute(statement).rowcount


def requeue_expired_leases(connection: Connection, error: str) -> int:
    """Running jobs whose worker vanished: back into the queue, due at once."""
    statement = (
        update(Job)
        .where(*_lease_expired())
        .values(status=JobStatus.QUEUED, last_error=error, run_after=func.now(), updated_at=func.now(), **_CLEARED_LEASE)
    )
    return connection.execute(statement).rowcount


def claim_next(connection: Connection, kinds: Iterable[str], owner: str, lease_seconds: float) -> ClaimedJob | None:
    """Take the next due job of one of these kinds. SKIP LOCKED: concurrent workers never get the same job."""
    next_due = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= func.now(), Job.kind.in_(list(kinds)))
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    statement = (
        update(Job)
        .where(Job.id == next_due)
        .values(
            status=JobStatus.RUNNING,
            attempts=Job.attempts + 1,
            lease_token=uuid.uuid4(),
            lease_owner=owner,
            lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
            started_at=func.now(),
            updated_at=func.now(),
        )
        .returning(Job.id, Job.kind, Job.payload, Job.attempts, Job.max_attempts, Job.lease_token)
    )
    row = connection.execute(statement).one_or_none()
    return ClaimedJob(**row._mapping) if row else None


def renew_lease(connection: Connection, job: ClaimedJob, lease_seconds: float) -> bool:
    # Leaves updated_at alone: a renewal is no visible change, "Aktualisiert" stays at the last real one.
    statement = (
        update(Job).where(*_still_leased(job)).values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
    )
    return connection.execute(statement).rowcount == 1


def lock_if_leased(connection: Connection, job: ClaimedJob) -> bool:
    """Lock the job row for the rest of the transaction, if the lease is still this worker's."""
    statement = select(Job.id).where(*_still_leased(job)).with_for_update()
    return connection.execute(statement).first() is not None


def mark_succeeded(connection: Connection, job: ClaimedJob) -> bool:
    return _update_leased(connection, job, status=JobStatus.SUCCEEDED, finished_at=func.now(), **_CLEARED_LEASE)


def schedule_retry(connection: Connection, job: ClaimedJob, delay_seconds: float, error: str) -> bool:
    run_after = func.now() + timedelta(seconds=delay_seconds)
    return _update_leased(connection, job, status=JobStatus.QUEUED, run_after=run_after, last_error=error, **_CLEARED_LEASE)


def mark_failed(connection: Connection, job: ClaimedJob, error: str) -> bool:
    return _update_leased(
        connection, job, status=JobStatus.FAILED, last_error=error, finished_at=func.now(), **_CLEARED_LEASE
    )


def release(connection: Connection, job: ClaimedJob) -> bool:
    """Back into the queue without counting the attempt: the job was interrupted before doing its work."""
    return _update_leased(
        connection, job, status=JobStatus.QUEUED, attempts=Job.attempts - 1, run_after=func.now(), **_CLEARED_LEASE
    )


def count_by_status(connection: Connection) -> dict[str, int]:
    rows = connection.execute(select(Job.status, func.count()).group_by(Job.status))
    return {status: count for status, count in rows}


def latest(connection: Connection, status: str | None, limit: int) -> Sequence[Row]:
    statement = select(Job.__table__).order_by(Job.id.desc()).limit(limit)
    if status:
        statement = statement.where(Job.status == status)
    return connection.execute(statement).all()


def lock_jobs(connection: Connection, job_ids: Iterable[int]) -> Sequence[Row]:
    statement = select(Job.id, Job.status, Job.dedupe_key).where(Job.id.in_(list(job_ids))).order_by(Job.id)
    return connection.execute(statement.with_for_update()).all()


def active_dedupe_keys(connection: Connection, dedupe_keys: Iterable[str]) -> set[str]:
    statement = select(Job.dedupe_key).where(Job.dedupe_key.in_(list(dedupe_keys)), Job.status.in_(ACTIVE_JOB_STATUSES))
    return set(connection.execute(statement).scalars())


def requeue(connection: Connection, job_ids: Iterable[int]) -> int:
    """Waiting again with a fresh set of attempts. The last error stays visible until the next attempt."""
    statement = (
        update(Job)
        .where(Job.id.in_(list(job_ids)))
        .values(
            status=JobStatus.QUEUED,
            attempts=0,
            run_after=func.now(),
            finished_at=None,
            updated_at=func.now(),
            **_CLEARED_LEASE,
        )
    )
    return connection.execute(statement).rowcount


def delete_finished(connection: Connection, status: JobStatus, older_than_days: int) -> int:
    statement = delete(Job).where(
        Job.status == status, Job.finished_at < func.now() - timedelta(days=older_than_days)
    )
    return connection.execute(statement).rowcount


def _lease_expired():
    return (Job.status == JobStatus.RUNNING, Job.lease_expires_at < func.now())


def _still_leased(job: ClaimedJob):
    return (Job.id == job.id, Job.status == JobStatus.RUNNING, Job.lease_token == job.lease_token)


def _update_leased(connection: Connection, job: ClaimedJob, **values) -> bool:
    """Write only while the lease is still this worker's (fencing token); False once another worker took over."""
    statement = update(Job).where(*_still_leased(job)).values(updated_at=func.now(), **values)
    return connection.execute(statement).rowcount == 1
