"""source query: source.query, the search of an adapter that needs one (GDELT)

Revision ID: e2a68a4ed2e0
Revises: 856511d67c19
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2a68a4ed2e0"
down_revision: str | Sequence[str] | None = "856511d67c19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source", sa.Column("query", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("source", "query")
