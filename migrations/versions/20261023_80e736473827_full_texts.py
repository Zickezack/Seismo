"""full texts: tables raw_text and fetch_host, article.full_text_status and full_text_error,
platform_settings.domain_min_interval_seconds

Revision ID: 80e736473827
Revises: 2eb9974bce2d
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "80e736473827"
down_revision: str | Sequence[str] | None = "2eb9974bce2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'pending', 'stored', 'disallowed', 'unsupported', 'failed', 'skipped'"


def upgrade() -> None:
    op.create_table(
        "fetch_host",
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("next_slot_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("robots_status", sa.String(length=20), nullable=True),
        sa.Column("robots_txt", sa.Text(), nullable=True),
        sa.Column("robots_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("robots_status IN ('rules', 'allow_all', 'disallow_all')", name=op.f("ck_fetch_host_robots_status")),
        sa.PrimaryKeyConstraint("host", name=op.f("pk_fetch_host")),
    )
    op.create_table(
        "raw_text",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"], ["article.id"], name=op.f("fk_raw_text_article_id_article"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("article_id", name=op.f("pk_raw_text")),
    )
    op.add_column("article", sa.Column("full_text_status", sa.String(length=20), server_default="pending", nullable=False))
    op.add_column("article", sa.Column("full_text_error", sa.Text(), nullable=True))
    # A migration queues no jobs, so articles stored before full texts existed never get theirs.
    op.execute(
        "UPDATE article SET full_text_status = 'skipped', "
        "full_text_error = 'Abgerufen, bevor es Volltexte gab'"
    )
    op.create_check_constraint(op.f("ck_article_full_text_status"), "article", f"full_text_status IN ({STATUSES})")
    op.add_column(
        "platform_settings", sa.Column("domain_min_interval_seconds", sa.Integer(), server_default="10", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_platform_settings_domain_min_interval_seconds"), "platform_settings", "domain_min_interval_seconds >= 1"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_domain_min_interval_seconds"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "domain_min_interval_seconds")
    op.drop_constraint(op.f("ck_article_full_text_status"), "article", type_="check")
    op.drop_column("article", "full_text_error")
    op.drop_column("article", "full_text_status")
    op.drop_table("raw_text")
    op.drop_table("fetch_host")
