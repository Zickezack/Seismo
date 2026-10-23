"""backfill: table backfill, article.backfill_id, job.priority with the claim index on (status, priority, run_after)

Revision ID: d29ed1e9b63f
Revises: e2a68a4ed2e0
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d29ed1e9b63f"
down_revision: str | Sequence[str] | None = "e2a68a4ed2e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backfill",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("articles_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("articles_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('running', 'done', 'failed', 'cancelled')", name=op.f("ck_backfill_status")),
        sa.CheckConstraint("period_start < period_end", name=op.f("ck_backfill_period")),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name=op.f("fk_backfill_source_id_source"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backfill")),
    )
    op.create_index(op.f("ix_backfill_source_id"), "backfill", ["source_id"])
    op.add_column("article", sa.Column("backfill_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_article_backfill_id"), "article", ["backfill_id"])
    op.create_foreign_key(
        op.f("fk_article_backfill_id_backfill"), "article", "backfill", ["backfill_id"], ["id"], ondelete="SET NULL"
    )
    # Every job queued so far is live work.
    op.add_column("job", sa.Column("priority", sa.SmallInteger(), server_default="0", nullable=False))
    op.create_check_constraint(op.f("ck_job_priority"), "job", "priority >= 0")
    op.drop_index("ix_job_status_run_after", table_name="job")
    op.create_index("ix_job_status_priority_run_after", "job", ["status", "priority", "run_after"])


def downgrade() -> None:
    op.drop_index("ix_job_status_priority_run_after", table_name="job")
    op.create_index("ix_job_status_run_after", "job", ["status", "run_after"])
    op.drop_constraint(op.f("ck_job_priority"), "job", type_="check")
    op.drop_column("job", "priority")
    op.drop_constraint(op.f("fk_article_backfill_id_backfill"), "article", type_="foreignkey")
    op.drop_index(op.f("ix_article_backfill_id"), table_name="article")
    op.drop_column("article", "backfill_id")
    op.drop_index(op.f("ix_backfill_source_id"), table_name="backfill")
    op.drop_table("backfill")
