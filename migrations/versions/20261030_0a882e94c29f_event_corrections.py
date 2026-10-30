"""event corrections: event_correction (Korrektur), the audit trail of merges and splits; event.founding_candidate_id,
merged_into_id; event_state.added_by_id, withdrawn_by_id

Revision ID: 0a882e94c29f
Revises: 5d0c7e1a9b42
Create Date: 2026-10-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0a882e94c29f"
down_revision: str | Sequence[str] | None = "5d0c7e1a9b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_correction",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("moves", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('merge', 'split')", name=op.f("ck_event_correction_kind")),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], name=op.f("fk_event_correction_event_id_event"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_correction")),
    )
    op.create_index(op.f("ix_event_correction_event_id"), "event_correction", ["event_id"])
    op.add_column("event", sa.Column("founding_candidate_id", sa.BigInteger(), nullable=True))
    op.add_column("event", sa.Column("merged_into_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_event_founding_candidate_id"), "event", ["founding_candidate_id"])
    op.create_foreign_key(
        op.f("fk_event_founding_candidate_id_event_candidate"), "event", "event_candidate",
        ["founding_candidate_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(op.f("fk_event_merged_into_id_event"), "event", "event", ["merged_into_id"], ["id"], ondelete="SET NULL")
    # The report each event is named after: the one with its title, which the report that founded it gave it - the
    # founding one first, should another have the same title.
    op.execute("""
        UPDATE event e SET founding_candidate_id = (
            SELECT c.id FROM event_candidate c
            WHERE c.event_id = e.id AND c.title = e.title
            ORDER BY c.assignment_method = 'new' DESC, c.id
            LIMIT 1
        )
    """)
    op.add_column("event_state", sa.Column("added_by_id", sa.BigInteger(), nullable=True))
    op.add_column("event_state", sa.Column("withdrawn_by_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_event_state_added_by_id_event_correction"), "event_state", "event_correction", ["added_by_id"], ["id"]
    )
    op.create_foreign_key(
        op.f("fk_event_state_withdrawn_by_id_event_correction"), "event_state", "event_correction", ["withdrawn_by_id"], ["id"]
    )


def downgrade() -> None:
    # The code before knows no withdrawn version: it would take one for in effect. Merged events stay, without reports.
    op.execute("DELETE FROM event_state WHERE withdrawn_by_id IS NOT NULL")
    op.drop_constraint(op.f("fk_event_state_withdrawn_by_id_event_correction"), "event_state", type_="foreignkey")
    op.drop_constraint(op.f("fk_event_state_added_by_id_event_correction"), "event_state", type_="foreignkey")
    op.drop_column("event_state", "withdrawn_by_id")
    op.drop_column("event_state", "added_by_id")
    op.drop_constraint(op.f("fk_event_merged_into_id_event"), "event", type_="foreignkey")
    op.drop_constraint(op.f("fk_event_founding_candidate_id_event_candidate"), "event", type_="foreignkey")
    op.drop_index(op.f("ix_event_founding_candidate_id"), table_name="event")
    op.drop_column("event", "merged_into_id")
    op.drop_column("event", "founding_candidate_id")
    op.drop_index(op.f("ix_event_correction_event_id"), table_name="event_correction")
    op.drop_table("event_correction")
