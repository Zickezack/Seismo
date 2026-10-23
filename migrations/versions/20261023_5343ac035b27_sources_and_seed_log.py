"""sources and seed log: tables source and seed_log

Revision ID: 5343ac035b27
Revises: 5c3345907ad0
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5343ac035b27"
down_revision: str | Sequence[str] | None = "5c3345907ad0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No seed rows here: the starter catalogue is imported only by an admin action (architecture.md, "Seed Data").
    op.create_table(
        "seed_log",
        sa.Column("seed_source", sa.String(length=100), nullable=False),
        sa.Column("item_key", sa.String(length=200), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("seed_source", "item_key", name=op.f("pk_seed_log")),
    )
    op.create_table(
        "source",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("adapter_type", sa.String(length=20), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("fetch_interval_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("terms_note", sa.Text(), nullable=True),
        sa.Column("raw_text_retention_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("fetch_interval_minutes >= 1", name=op.f("ck_source_fetch_interval_minutes")),
        sa.CheckConstraint("raw_text_retention_days >= 0", name=op.f("ck_source_raw_text_retention_days")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source")),
    )
    op.create_index("uq_source_name", "source", [sa.literal_column("lower(name)")], unique=True)


def downgrade() -> None:
    op.drop_index("uq_source_name", table_name="source")
    op.drop_table("source")
    op.drop_table("seed_log")
