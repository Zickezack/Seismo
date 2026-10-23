"""tdm reservation: article.full_text_status 'reserved', the tdmrep.json cache in fetch_host,
platform_settings.tdm_blocked_domains

Revision ID: 3152564cbc2b
Revises: b573f2c351f0
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3152564cbc2b"
down_revision: str | Sequence[str] | None = "b573f2c351f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'pending', 'stored', 'disallowed', 'unsupported', 'failed', 'blocked', 'skipped'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_article_full_text_status"), "article", type_="check")
    op.create_check_constraint(
        op.f("ck_article_full_text_status"), "article", f"full_text_status IN ({STATUSES}, 'reserved')"
    )
    op.add_column("fetch_host", sa.Column("tdmrep_status", sa.String(length=20), nullable=True))
    op.add_column("fetch_host", sa.Column("tdmrep_rules", sa.Text(), nullable=True))
    op.add_column("fetch_host", sa.Column("tdmrep_fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(op.f("ck_fetch_host_tdmrep_status"), "fetch_host", "tdmrep_status IN ('rules', 'none')")
    # NULL: the list the code starts with (platform_settings_service), like every setting never saved.
    op.add_column("platform_settings", sa.Column("tdm_blocked_domains", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("platform_settings", "tdm_blocked_domains")
    op.drop_constraint(op.f("ck_fetch_host_tdmrep_status"), "fetch_host", type_="check")
    op.drop_column("fetch_host", "tdmrep_fetched_at")
    op.drop_column("fetch_host", "tdmrep_rules")
    op.drop_column("fetch_host", "tdmrep_status")
    # Reserved full texts were never stored; before this revision the closest outcome is failed.
    op.execute("UPDATE article SET full_text_status = 'failed' WHERE full_text_status = 'reserved'")
    op.drop_constraint(op.f("ck_article_full_text_status"), "article", type_="check")
    op.create_check_constraint(op.f("ck_article_full_text_status"), "article", f"full_text_status IN ({STATUSES})")
