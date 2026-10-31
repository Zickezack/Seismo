"""raw text deletion: article.raw_text_deleted_at, when its raw text was deleted after its source's retention period

Revision ID: f58df57d5e12
Revises: 3201aa52f946
Create Date: 2026-10-31

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f58df57d5e12"
down_revision: str | Sequence[str] | None = "3201aa52f946"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("article", sa.Column("raw_text_deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # A deleted raw text stays deleted; the code before only no longer says when.
    op.drop_column("article", "raw_text_deleted_at")
