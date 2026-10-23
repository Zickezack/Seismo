"""SQLAlchemy models. Documented in datamodel.md, "Entities"; changed only via skill datenmodell-aendern."""
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, SmallInteger, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# A job with a dedupe key exists only once while it is waiting or running.
ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class Job(Base):
    """A unit of work in the PostgreSQL job queue (UI term: Job)."""

    __tablename__ = "job"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(100))
    # IDs only, never names or texts (datamodel.md, rule 13).
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), server_default=JobStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(server_default="0")
    max_attempts: Mapped[int] = mapped_column(server_default="5")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set on every claim; a worker may only write to the job while its token is still the current one.
    lease_token: Mapped[uuid.UUID | None]
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(status.in_([s.value for s in JobStatus]), name="status"),
        CheckConstraint(attempts >= 0, name="attempts"),
        CheckConstraint(max_attempts >= 1, name="max_attempts"),
        Index("ix_job_status_run_after", status, run_after),
        Index(
            "uq_job_dedupe_key_active",
            dedupe_key,
            unique=True,
            postgresql_where=status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
        ),
    )


class PlatformSettingsRecord(Base):
    """Plattform-Einstellungen: one row for the whole instance (requirements.md, "Settings")."""

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False, server_default="1")
    llm_max_parallel_calls: Mapped[int] = mapped_column(server_default="1")
    succeeded_jobs_retention_days: Mapped[int] = mapped_column(server_default="7")
    failed_jobs_retention_days: Mapped[int] = mapped_column(server_default="30")
    backup_interval_hours: Mapped[int] = mapped_column(server_default="24")
    backups_to_keep: Mapped[int] = mapped_column(server_default="14")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(id == 1, name="single_row"),
        CheckConstraint(llm_max_parallel_calls >= 1, name="llm_max_parallel_calls"),
        CheckConstraint(succeeded_jobs_retention_days >= 1, name="succeeded_jobs_retention_days"),
        CheckConstraint(failed_jobs_retention_days >= 1, name="failed_jobs_retention_days"),
        CheckConstraint(backup_interval_hours >= 1, name="backup_interval_hours"),
        CheckConstraint(backups_to_keep >= 1, name="backups_to_keep"),
    )
