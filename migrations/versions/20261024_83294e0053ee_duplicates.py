"""duplicates: article.content_hash, duplicate_of_id, duplicate_reason; a hash index on article.canonical_url

Revision ID: 83294e0053ee
Revises: eba26a55a0f5
Create Date: 2026-10-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "83294e0053ee"
down_revision: str | Sequence[str] | None = "eba26a55a0f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("article", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.add_column("article", sa.Column("duplicate_of_id", sa.BigInteger(), nullable=True))
    op.add_column("article", sa.Column("duplicate_reason", sa.String(length=10), nullable=True))
    op.create_check_constraint(op.f("ck_article_duplicate_reason"), "article", "duplicate_reason IN ('url', 'text')")
    op.create_foreign_key(
        op.f("fk_article_duplicate_of_id_article"), "article", "article", ["duplicate_of_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_article_duplicate_of_id"), "article", ["duplicate_of_id"])
    op.create_index(op.f("ix_article_content_hash"), "article", ["content_hash"])
    op.create_index("ix_article_canonical_url", "article", ["canonical_url"], postgresql_using="hash")
    # Normalized once more, so the duplicates among the articles stored so far are recognised (no request).
    op.execute("UPDATE article SET normalization_status = 'pending', normalized_at = NULL WHERE normalization_status = 'done'")


def downgrade() -> None:
    op.drop_index("ix_article_canonical_url", table_name="article", postgresql_using="hash")
    op.drop_index(op.f("ix_article_content_hash"), table_name="article")
    op.drop_index(op.f("ix_article_duplicate_of_id"), table_name="article")
    op.drop_constraint(op.f("fk_article_duplicate_of_id_article"), "article", type_="foreignkey")
    op.drop_constraint(op.f("ck_article_duplicate_reason"), "article", type_="check")
    op.drop_column("article", "duplicate_reason")
    op.drop_column("article", "duplicate_of_id")
    op.drop_column("article", "content_hash")
