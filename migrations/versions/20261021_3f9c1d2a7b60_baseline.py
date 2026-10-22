"""baseline: empty schema, so every later migration has a common starting point

Revision ID: 3f9c1d2a7b60
Revises:
Create Date: 2026-10-21

"""
from collections.abc import Sequence

revision: str = "3f9c1d2a7b60"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
