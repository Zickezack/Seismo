"""normalization: article.canonical_url, published_at_source, detected_language, normalization_status/_error,
normalized_at; raw_text.plain_text

Revision ID: eba26a55a0f5
Revises: d2bbb146df5d
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "eba26a55a0f5"
down_revision: str | Sequence[str] | None = "d2bbb146df5d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("article", sa.Column("canonical_url", sa.String(length=2000), nullable=True))
    op.add_column("article", sa.Column("published_at_source", sa.String(length=10), nullable=True))
    op.add_column("article", sa.Column("detected_language", sa.String(length=10), nullable=True))
    # Every article stored so far is normalized by the worker once its full text is settled.
    op.add_column(
        "article", sa.Column("normalization_status", sa.String(length=20), server_default="pending", nullable=False)
    )
    op.add_column("article", sa.Column("normalization_error", sa.Text(), nullable=True))
    op.add_column("article", sa.Column("normalized_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_article_normalization_status"), "article", "normalization_status IN ('pending', 'done', 'failed')"
    )
    op.create_check_constraint(
        op.f("ck_article_published_at_source"), "article", "published_at_source IN ('feed', 'gdelt', 'page')"
    )
    op.create_index(
        "ix_article_normalization_pending", "article", ["id"], postgresql_where=sa.text("normalization_status = 'pending'")
    )
    # Where the publication times stored so far came from: GDELT's seendate, else the feed.
    op.execute(
        "UPDATE article SET published_at_source = CASE WHEN source.adapter_type = 'gdelt' THEN 'gdelt' ELSE 'feed' END "
        "FROM source WHERE source.id = article.source_id AND article.published_at IS NOT NULL"
    )
    op.execute(
        "UPDATE article SET published_at_source = 'feed' WHERE published_at IS NOT NULL AND published_at_source IS NULL"
    )
    op.add_column("raw_text", sa.Column("plain_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("raw_text", "plain_text")
    op.drop_index("ix_article_normalization_pending", table_name="article", postgresql_where=sa.text("normalization_status = 'pending'"))
    op.drop_constraint(op.f("ck_article_published_at_source"), "article", type_="check")
    op.drop_constraint(op.f("ck_article_normalization_status"), "article", type_="check")
    op.drop_column("article", "normalized_at")
    op.drop_column("article", "normalization_error")
    op.drop_column("article", "normalization_status")
    op.drop_column("article", "detected_language")
    op.drop_column("article", "published_at_source")
    op.drop_column("article", "canonical_url")
