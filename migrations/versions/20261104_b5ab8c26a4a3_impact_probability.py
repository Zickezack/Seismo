"""impact probability: impact_hypothesis_version.probability, proposed_probability, abstention, expected,
expected_support and the target's move before the event (Markt- und Wirkungsmodell, story 2)

Revision ID: b5ab8c26a4a3
Revises: cc8ef9040619
Create Date: 2026-11-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5ab8c26a4a3"
down_revision: str | Sequence[str] | None = "cc8ef9040619"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "impact_hypothesis_version"
CHECKS = {
    "probability": "probability IN ('high', 'medium', 'low')",
    "proposed_probability": "proposed_probability IN ('high', 'medium', 'low')",
    "expected": "expected IN ('yes', 'no', 'unknown')",
    "abstention": "probability IS NULL OR abstention IS NULL",
}


def upgrade() -> None:
    # The versions so far stay as they are: without a probability (UI: nicht bewertet).
    op.add_column(TABLE, sa.Column("probability", sa.String(length=10), nullable=True))
    op.add_column(TABLE, sa.Column("proposed_probability", sa.String(length=10), nullable=True))
    op.add_column(TABLE, sa.Column("abstention", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("expected", sa.String(length=10), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("expected_support", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column(TABLE, sa.Column("pre_instrument_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("pre_abnormal", sa.Float(), nullable=True))
    op.add_column(TABLE, sa.Column("pre_t", sa.Float(), nullable=True))
    op.add_column(TABLE, sa.Column("priced_in", sa.Boolean(), nullable=True))
    op.create_foreign_key(
        op.f("fk_impact_hypothesis_version_pre_instrument_id_instrument"), TABLE, "instrument", ["pre_instrument_id"], ["id"],
        ondelete="SET NULL",
    )
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, condition)


def downgrade() -> None:
    for name in CHECKS:
        op.drop_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, type_="check")
    op.drop_constraint(op.f("fk_impact_hypothesis_version_pre_instrument_id_instrument"), TABLE, type_="foreignkey")
    for column in ("priced_in", "pre_t", "pre_abnormal", "pre_instrument_id", "expected_support", "expected", "abstention",
                   "proposed_probability", "probability"):
        op.drop_column(TABLE, column)
