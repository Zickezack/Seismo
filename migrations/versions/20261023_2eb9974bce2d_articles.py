"""articles: table article, source.next_fetch_at and last_fetched_at, platform_settings.article_max_age_days

Revision ID: 2eb9974bce2d
Revises: 5343ac035b27
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2eb9974bce2d"
down_revision: str | Sequence[str] | None = "5343ac035b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("source_name", sa.String(length=200), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("item_key", sa.String(length=1000), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name=op.f("fk_article_source_id_source"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_article")),
        sa.UniqueConstraint("source_id", "item_key", name=op.f("uq_article_source_id")),
    )
    op.create_index("ix_article_fetched_at", "article", ["fetched_at"], unique=False)
    # Existing sources are due at once; the server default fills an existing settings row.
    op.add_column("source", sa.Column("next_fetch_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("source", sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("platform_settings", sa.Column("article_max_age_days", sa.Integer(), server_default="7", nullable=False))
    op.create_check_constraint(
        op.f("ck_platform_settings_article_max_age_days"), "platform_settings", "article_max_age_days >= 1"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_article_max_age_days"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "article_max_age_days")
    op.drop_column("source", "last_fetched_at")
    op.drop_column("source", "next_fetch_at")
    op.drop_index("ix_article_fetched_at", table_name="article")
    op.drop_table("article")
