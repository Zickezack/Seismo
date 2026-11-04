"""event search: full-text indexes on the reports' title and summary and on the statements (Dashboard, Recherche und
Alerts, story 4)

Revision ID: 3c7e9a1f5b20
Revises: ff06b2d2f906
Create Date: 2026-11-04

"""
from collections.abc import Sequence

from alembic import op

revision: str = "3c7e9a1f5b20"
down_revision: str | Sequence[str] | None = "ff06b2d2f906"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The expressions of app.db.models.search_vector: a query uses an index only when it states the same one.
def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_event_candidate_search ON event_candidate "
        "USING gin (to_tsvector('english'::regconfig, (title || ' ') || summary))"
    )
    op.execute("CREATE INDEX ix_statement_search ON statement USING gin (to_tsvector('english'::regconfig, text))")


def downgrade() -> None:
    op.drop_index("ix_statement_search", table_name="statement")
    op.drop_index("ix_event_candidate_search", table_name="event_candidate")
