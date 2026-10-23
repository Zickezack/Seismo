"""backup settings: platform_settings.backup_interval_hours and backups_to_keep

Revision ID: 5c3345907ad0
Revises: 724a65db6143
Create Date: 2026-10-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c3345907ad0"
down_revision: str | Sequence[str] | None = "724a65db6143"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The server defaults fill an existing settings row.
    op.add_column("platform_settings", sa.Column("backup_interval_hours", sa.Integer(), server_default="24", nullable=False))
    op.add_column("platform_settings", sa.Column("backups_to_keep", sa.Integer(), server_default="14", nullable=False))
    op.create_check_constraint(
        op.f("ck_platform_settings_backup_interval_hours"), "platform_settings", "backup_interval_hours >= 1"
    )
    op.create_check_constraint(op.f("ck_platform_settings_backups_to_keep"), "platform_settings", "backups_to_keep >= 1")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_backups_to_keep"), "platform_settings", type_="check")
    op.drop_constraint(op.f("ck_platform_settings_backup_interval_hours"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "backups_to_keep")
    op.drop_column("platform_settings", "backup_interval_hours")
