"""event states: event_state (Ereigniszustand), the versions of an event's state; the assigned reports replayed

Revision ID: 5d0c7e1a9b42
Revises: 940fb34413a5
Create Date: 2026-10-29

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d0c7e1a9b42"
down_revision: str | Sequence[str] | None = "940fb34413a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = ("start", "update", "escalation", "closure", "resumption")


def upgrade() -> None:
    op.create_table(
        "event_state",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(value) for value in STATES)})", name=op.f("ck_event_state_status")
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["event_candidate.id"],
            name=op.f("fk_event_state_candidate_id_event_candidate"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], name=op.f("fk_event_state_event_id_event"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_state")),
    )
    op.create_index(op.f("ix_event_state_candidate_id"), "event_state", ["candidate_id"])
    op.create_index("ix_event_state_event_id_valid_from", "event_state", ["event_id", "valid_from"])
    _replay_assigned_reports()


def downgrade() -> None:
    op.drop_index("ix_event_state_event_id_valid_from", table_name="event_state")
    op.drop_index(op.f("ix_event_state_candidate_id"), table_name="event_state")
    op.drop_table("event_state")


def _replay_assigned_reports() -> None:
    """Give the events assigned so far the versions they would have got: every assigned report in the order it was
    assigned, recorded when it was. A copy of event_service.state_effect as of this revision, so a later change of
    the rule never changes what this migration did."""
    connection = op.get_bind()
    reports = connection.execute(sa.text("""
        SELECT c.id, c.event_id, c.status, coalesce(a.published_at, a.fetched_at) AS moment, c.assigned_at
        FROM event_candidate c JOIN article a ON a.id = c.article_id
        WHERE c.event_id IS NOT NULL
        ORDER BY c.assigned_at, c.id
    """)).all()
    versions: dict[int, list[tuple]] = {}
    insert = sa.text("""
        INSERT INTO event_state (event_id, status, valid_from, candidate_id, recorded_at)
        VALUES (:event_id, :status, :valid_from, :candidate_id, :recorded_at)
    """)
    for report in reports:
        timeline = versions.setdefault(report.event_id, [])
        in_effect = [status for valid_from, status in timeline if valid_from <= report.moment]
        if _starts_version(in_effect[-1] if in_effect else None, report.status):
            values = {"event_id": report.event_id, "status": report.status, "valid_from": report.moment}
            connection.execute(insert, values | {"candidate_id": report.id, "recorded_at": report.assigned_at})
            timeline.append((report.moment, report.status))
            timeline.sort(key=lambda version: version[0])


def _starts_version(current: str | None, reported: str) -> bool:
    if reported == "unknown":
        return False
    if current is None:
        return True
    if reported == current:
        return False
    if current == "closure":
        return reported == "resumption"
    return reported in ("escalation", "closure")
