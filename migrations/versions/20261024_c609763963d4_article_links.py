"""article links: article_link (similar texts, pairs the admin separated); article.text_signature and text_bands

Revision ID: c609763963d4
Revises: 83294e0053ee
Create Date: 2026-10-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c609763963d4"
down_revision: str | Sequence[str] | None = "83294e0053ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_link",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("other_id", sa.BigInteger(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("separated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("article_id < other_id", name=op.f("ck_article_link_ordered_pair")),
        sa.ForeignKeyConstraint(
            ["article_id"], ["article.id"], name=op.f("fk_article_link_article_id_article"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["other_id"], ["article.id"], name=op.f("fk_article_link_other_id_article"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("article_id", "other_id", name=op.f("pk_article_link")),
    )
    op.create_index(op.f("ix_article_link_other_id"), "article_link", ["other_id"])
    op.add_column("article", sa.Column("text_signature", postgresql.ARRAY(sa.Integer()), nullable=True))
    op.add_column("article", sa.Column("text_bands", postgresql.ARRAY(sa.BigInteger()), nullable=True))
    op.create_index("ix_article_text_bands", "article", ["text_bands"], postgresql_using="gin")
    # Normalized once more, so the articles stored so far get their signature and their links (no request).
    op.execute("UPDATE article SET normalization_status = 'pending', normalized_at = NULL WHERE normalization_status = 'done'")


def downgrade() -> None:
    op.drop_index("ix_article_text_bands", table_name="article", postgresql_using="gin")
    op.drop_column("article", "text_bands")
    op.drop_column("article", "text_signature")
    op.drop_index(op.f("ix_article_link_other_id"), table_name="article_link")
    op.drop_table("article_link")
