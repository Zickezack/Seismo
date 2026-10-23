"""source fetch status: source.min_request_interval_seconds, last_error, last_error_at and
consecutive_failures, fetch_host.paused_until

Revision ID: 856511d67c19
Revises: 80e736473827
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "856511d67c19"
down_revision: str | Sequence[str] | None = "80e736473827"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fetch_host", sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("source", sa.Column("min_request_interval_seconds", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_source_min_request_interval_seconds"), "source", "min_request_interval_seconds >= 1"
    )
    op.add_column("source", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("source", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("source", sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("source", "consecutive_failures")
    op.drop_column("source", "last_error_at")
    op.drop_column("source", "last_error")
    op.drop_constraint(op.f("ck_source_min_request_interval_seconds"), "source", type_="check")
    op.drop_column("source", "min_request_interval_seconds")
    op.drop_column("fetch_host", "paused_until")
