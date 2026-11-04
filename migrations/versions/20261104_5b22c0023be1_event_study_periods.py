"""event study periods: platform_settings.study_training_until and study_validation_until (Historische Markt- und
Ereignisdaten, story 2)

Revision ID: 5b22c0023be1
Revises: 8fc0aafe5eb6
Create Date: 2026-11-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b22c0023be1"
down_revision: str | Sequence[str] | None = "8fc0aafe5eb6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both empty: the events are not divided into periods yet.
    op.add_column("platform_settings", sa.Column("study_training_until", sa.Date(), nullable=True))
    op.add_column("platform_settings", sa.Column("study_validation_until", sa.Date(), nullable=True))
    op.create_check_constraint(
        op.f("ck_platform_settings_study_periods"),
        "platform_settings",
        "study_validation_until IS NULL OR (study_training_until IS NOT NULL AND study_validation_until > study_training_until)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_study_periods"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "study_validation_until")
    op.drop_column("platform_settings", "study_training_until")
