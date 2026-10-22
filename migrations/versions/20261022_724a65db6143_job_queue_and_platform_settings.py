"""job queue and platform settings: tables job and platform_settings

Revision ID: 724a65db6143
Revises: 3f9c1d2a7b60
Create Date: 2026-10-22

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "724a65db6143"
down_revision: str | Sequence[str] | None = "3f9c1d2a7b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name=op.f("ck_job_status")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_job_attempts")),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_job_max_attempts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
    )
    op.create_index("ix_job_status_run_after", "job", ["status", "run_after"], unique=False)
    op.create_index(
        "uq_job_dedupe_key_active",
        "job",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "platform_settings",
        sa.Column("id", sa.SmallInteger(), server_default="1", autoincrement=False, nullable=False),
        sa.Column("llm_max_parallel_calls", sa.Integer(), server_default="1", nullable=False),
        sa.Column("succeeded_jobs_retention_days", sa.Integer(), server_default="7", nullable=False),
        sa.Column("failed_jobs_retention_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_platform_settings_single_row")),
        sa.CheckConstraint("llm_max_parallel_calls >= 1", name=op.f("ck_platform_settings_llm_max_parallel_calls")),
        sa.CheckConstraint(
            "succeeded_jobs_retention_days >= 1", name=op.f("ck_platform_settings_succeeded_jobs_retention_days")
        ),
        sa.CheckConstraint("failed_jobs_retention_days >= 1", name=op.f("ck_platform_settings_failed_jobs_retention_days")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_settings")),
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
    op.drop_index("uq_job_dedupe_key_active", table_name="job", postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_index("ix_job_status_run_after", table_name="job")
    op.drop_table("job")
