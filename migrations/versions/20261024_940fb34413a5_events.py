"""events: event (Ereignis, its stable id); event_candidate.event_id, assignment_method, assignment_score, assigned_at

Revision ID: 940fb34413a5
Revises: 29544a69c420
Create Date: 2026-10-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "940fb34413a5"
down_revision: str | Sequence[str] | None = "29544a69c420"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPES = (
    "armed_conflict", "terrorism", "civil_unrest", "natural_disaster", "accident", "cyber_incident",
    "infrastructure_outage", "supply_chain_disruption", "energy_supply", "commodity_market", "monetary_policy",
    "fiscal_policy", "trade_policy", "regulation", "election_politics", "diplomacy", "corporate", "labor", "health",
    "legal", "economic_data", "other",
)


def upgrade() -> None:
    op.create_table(
        "event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("first_reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            f"event_type IN ({', '.join(repr(value) for value in EVENT_TYPES)})", name=op.f("ck_event_event_type")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
    )
    op.create_index(op.f("ix_event_last_reported_at"), "event", ["last_reported_at"])
    # Every candidate stored so far is assigned by the worker, the oldest report first (event_service.enqueue_due).
    op.add_column("event_candidate", sa.Column("event_id", sa.BigInteger(), nullable=True))
    op.add_column("event_candidate", sa.Column("assignment_method", sa.String(length=10), nullable=True))
    op.add_column("event_candidate", sa.Column("assignment_score", sa.Float(), nullable=True))
    op.add_column("event_candidate", sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_event_candidate_assignment_method"), "event_candidate", "assignment_method IN ('new', 'score', 'llm')"
    )
    op.create_foreign_key(
        op.f("fk_event_candidate_event_id_event"), "event_candidate", "event", ["event_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_event_candidate_event_id"), "event_candidate", ["event_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_event_candidate_event_id"), table_name="event_candidate")
    op.drop_constraint(op.f("fk_event_candidate_event_id_event"), "event_candidate", type_="foreignkey")
    op.drop_constraint(op.f("ck_event_candidate_assignment_method"), "event_candidate", type_="check")
    op.drop_column("event_candidate", "assigned_at")
    op.drop_column("event_candidate", "assignment_score")
    op.drop_column("event_candidate", "assignment_method")
    op.drop_column("event_candidate", "event_id")
    op.drop_index(op.f("ix_event_last_reported_at"), table_name="event")
    op.drop_table("event")
