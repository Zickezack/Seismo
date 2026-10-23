"""full text blocked: article.full_text_status 'blocked' for a host that refused the full text (HTTP 401, 403)

Revision ID: b573f2c351f0
Revises: d29ed1e9b63f
Create Date: 2026-10-23

"""
from collections.abc import Sequence

from alembic import op

revision: str = "b573f2c351f0"
down_revision: str | Sequence[str] | None = "d29ed1e9b63f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'pending', 'stored', 'disallowed', 'unsupported', 'failed', 'skipped'"
REFUSED = "('HTTP 401', 'HTTP 403')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_article_full_text_status"), "article", type_="check")
    op.create_check_constraint(
        op.f("ck_article_full_text_status"), "article", f"full_text_status IN ({STATUSES}, 'blocked')"
    )
    # Refused before this status existed: blocked as well, so the admin can fetch them again.
    op.execute(f"UPDATE article SET full_text_status = 'blocked' WHERE full_text_status = 'failed' AND full_text_error IN {REFUSED}")


def downgrade() -> None:
    op.execute("UPDATE article SET full_text_status = 'failed' WHERE full_text_status = 'blocked'")
    op.drop_constraint(op.f("ck_article_full_text_status"), "article", type_="check")
    op.create_check_constraint(op.f("ck_article_full_text_status"), "article", f"full_text_status IN ({STATUSES})")
