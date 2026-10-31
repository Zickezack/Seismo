"""refetch oversized lookups: full texts that failed because a host answered its tdmrep.json or robots.txt with more
than the limit - most often a page of its own for an unknown path - are fetched again, behind live work

Since then a tdmrep.json above the limit counts as none and a robots.txt counts as far as its first 500 KiB
(fetch_policy_service); before, both failed the article. A data repair only: no schema change.

Revision ID: 3201aa52f946
Revises: fb4c52ab16d6
Create Date: 2026-10-31

"""
from collections.abc import Sequence

from alembic import op

revision: str = "3201aa52f946"
down_revision: str | Sequence[str] | None = "fb4c52ab16d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The two limits of the lookups (tdm_policy.TDMREP_MAX_BYTES, fetch_policy_service.ROBOTS_MAX_BYTES); a page itself
# may take 10 MB, so its own errors never read so.
OVERSIZED = "('Antwort grösser als 100 KB', 'Antwort grösser als 500 KB')"
# As ingestion_service.enqueue_full_texts queues them: job_service.FETCH_ARTICLE_MAX_ATTEMPTS, behind live work.
FETCH_ARTICLE_MAX_ATTEMPTS = 31
BACKFILL_PRIORITY = 1


def upgrade() -> None:
    op.execute(f"""
        WITH reset AS (
            UPDATE article SET full_text_status = 'pending', full_text_error = NULL
            WHERE full_text_status = 'failed' AND full_text_error IN {OVERSIZED}
            RETURNING id
        )
        INSERT INTO job (kind, payload, dedupe_key, max_attempts, priority)
        SELECT 'fetch_article', jsonb_build_object('article_id', id), 'fetch_article:' || id, {FETCH_ARTICLE_MAX_ATTEMPTS},
               {BACKFILL_PRIORITY}
        FROM reset
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    # Nothing to undo: a full text fetched again is as any other, and one still pending is fetched by the code before.
    pass
