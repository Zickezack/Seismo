"""extraction validation and confidence: every model run with its validation result; quotes for place and time;
extraction, inference and source quality levels; source.quality and the domain list of source qualities

Revision ID: 29544a69c420
Revises: 0129b1c02d83
Create Date: 2026-10-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "29544a69c420"
down_revision: str | Sequence[str] | None = "0129b1c02d83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEVELS = "('high', 'medium', 'low')"
# Table, column and constraint name of every level.
LEVEL_COLUMNS = (
    ("event_candidate", "inference_confidence"),
    ("statement", "extraction_confidence"),
    ("evidence", "source_quality"),
    ("source", "quality"),
)


def upgrade() -> None:
    op.add_column("event_candidate", sa.Column("location_quote", sa.Text(), nullable=True))
    op.add_column("event_candidate", sa.Column("time_quote", sa.Text(), nullable=True))
    op.add_column("event_candidate", sa.Column("inference_confidence", sa.String(length=10), nullable=True))
    op.add_column("statement", sa.Column("extraction_confidence", sa.String(length=10), nullable=True))
    op.add_column("evidence", sa.Column("source_quality", sa.String(length=10), nullable=True))
    op.add_column("source", sa.Column("quality", sa.String(length=10), nullable=True))
    for table, column in LEVEL_COLUMNS:
        op.create_check_constraint(op.f(f"ck_{table}_{column}"), table, f"{column} IN {LEVELS}")
    # Every extraction so far had a valid answer: no other was stored.
    op.add_column("extraction", sa.Column("validation_result", sa.String(length=20), server_default="valid", nullable=False))
    op.add_column("extraction", sa.Column("validation_errors", sa.Text(), nullable=True))
    op.add_column("extraction", sa.Column("raw_answer", sa.Text(), nullable=True))
    op.add_column("extraction", sa.Column("repaired", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_check_constraint(
        op.f("ck_extraction_validation_result"), "extraction", "validation_result IN ('valid', 'invalid', 'incomplete')"
    )
    op.add_column("platform_settings", sa.Column("source_quality_domains", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("platform_settings", "source_quality_domains")
    # Before, every stored extraction was a valid one; an invalid run would pass for an extraction without events.
    op.execute("DELETE FROM extraction WHERE validation_result <> 'valid'")
    op.drop_constraint(op.f("ck_extraction_validation_result"), "extraction", type_="check")
    op.drop_column("extraction", "repaired")
    op.drop_column("extraction", "raw_answer")
    op.drop_column("extraction", "validation_errors")
    op.drop_column("extraction", "validation_result")
    for table, column in LEVEL_COLUMNS:
        op.drop_constraint(op.f(f"ck_{table}_{column}"), table, type_="check")
    op.drop_column("source", "quality")
    op.drop_column("evidence", "source_quality")
    op.drop_column("statement", "extraction_confidence")
    op.drop_column("event_candidate", "inference_confidence")
    op.drop_column("event_candidate", "time_quote")
    op.drop_column("event_candidate", "location_quote")
