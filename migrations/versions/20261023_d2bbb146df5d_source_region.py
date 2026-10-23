"""source region: source.region, what a source reports on (continent, Middle East or worldwide)

Revision ID: d2bbb146df5d
Revises: 3152564cbc2b
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2bbb146df5d"
down_revision: str | Sequence[str] | None = "3152564cbc2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing sources stay without one: the admin sets it, or takes it from the starter catalogue.
    op.add_column("source", sa.Column("region", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("source", "region")
