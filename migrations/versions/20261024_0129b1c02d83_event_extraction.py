"""event extraction: extraction, event_candidate, statement, evidence; the pre-filter's platform settings and
source.extract_events

Revision ID: 0129b1c02d83
Revises: c609763963d4
Create Date: 2026-10-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0129b1c02d83"
down_revision: str | Sequence[str] | None = "c609763963d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPES = (
    "armed_conflict", "terrorism", "civil_unrest", "natural_disaster", "accident", "cyber_incident",
    "infrastructure_outage", "supply_chain_disruption", "energy_supply", "commodity_market", "monetary_policy",
    "fiscal_policy", "trade_policy", "regulation", "election_politics", "diplomacy", "corporate", "labor", "health",
    "legal", "economic_data", "other",
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.create_table(
        "extraction",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("input_chars", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("statements_dropped", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["article.id"], name=op.f("fk_extraction_article_id_article"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction")),
    )
    op.create_index(op.f("ix_extraction_article_id"), "extraction", ["article_id"])
    op.create_table(
        "event_candidate",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("extraction_id", sa.BigInteger(), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("place", sa.Text(), nullable=True),
        sa.Column("time_note", sa.Text(), nullable=True),
        sa.Column("actors", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("impacts", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.CheckConstraint(_in("event_type", EVENT_TYPES), name=op.f("ck_event_candidate_event_type")),
        sa.CheckConstraint(
            _in("status", ("start", "update", "escalation", "closure", "resumption", "unknown")),
            name=op.f("ck_event_candidate_status"),
        ),
        sa.ForeignKeyConstraint(
            ["article_id"], ["article.id"], name=op.f("fk_event_candidate_article_id_article"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"], ["extraction.id"], name=op.f("fk_event_candidate_extraction_id_extraction"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_candidate")),
    )
    op.create_index(op.f("ix_event_candidate_article_id"), "event_candidate", ["article_id"])
    op.create_index(op.f("ix_event_candidate_extraction_id"), "event_candidate", ["extraction_id"])
    op.create_table(
        "statement",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.CheckConstraint(
            _in("kind", ("fact", "claim", "source_forecast", "interpretation")), name=op.f("ck_statement_kind")
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["event_candidate.id"], name=op.f("fk_statement_candidate_id_event_candidate"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement")),
    )
    op.create_index(op.f("ix_statement_candidate_id"), "statement", ["candidate_id"])
    op.create_table(
        "evidence",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("statement_id", sa.BigInteger(), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["article.id"], name=op.f("fk_evidence_article_id_article"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["statement_id"], ["statement.id"], name=op.f("fk_evidence_statement_id_statement"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )
    op.create_index(op.f("ix_evidence_article_id"), "evidence", ["article_id"])
    op.create_index(op.f("ix_evidence_statement_id"), "evidence", ["statement_id"])
    op.add_column(
        "platform_settings",
        sa.Column("extraction_languages", sa.String(length=200), server_default="de,en,es,fr,it,nl,pt", nullable=False),
    )
    op.add_column("platform_settings", sa.Column("extraction_min_text_chars", sa.Integer(), server_default="500", nullable=False))
    op.add_column("platform_settings", sa.Column("extraction_max_age_days", sa.Integer(), server_default="3", nullable=False))
    op.add_column("platform_settings", sa.Column("extractions_per_hour", sa.Integer(), server_default="30", nullable=False))
    op.create_check_constraint(
        op.f("ck_platform_settings_extraction_min_text_chars"), "platform_settings", "extraction_min_text_chars >= 0"
    )
    op.create_check_constraint(
        op.f("ck_platform_settings_extraction_max_age_days"), "platform_settings", "extraction_max_age_days >= 1"
    )
    op.create_check_constraint(op.f("ck_platform_settings_extractions_per_hour"), "platform_settings", "extractions_per_hour >= 0")
    op.add_column("source", sa.Column("extract_events", sa.Boolean(), server_default=sa.text("true"), nullable=False))


def downgrade() -> None:
    op.drop_column("source", "extract_events")
    op.drop_constraint(op.f("ck_platform_settings_extractions_per_hour"), "platform_settings", type_="check")
    op.drop_constraint(op.f("ck_platform_settings_extraction_max_age_days"), "platform_settings", type_="check")
    op.drop_constraint(op.f("ck_platform_settings_extraction_min_text_chars"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "extractions_per_hour")
    op.drop_column("platform_settings", "extraction_max_age_days")
    op.drop_column("platform_settings", "extraction_min_text_chars")
    op.drop_column("platform_settings", "extraction_languages")
    op.drop_index(op.f("ix_evidence_statement_id"), table_name="evidence")
    op.drop_index(op.f("ix_evidence_article_id"), table_name="evidence")
    op.drop_table("evidence")
    op.drop_index(op.f("ix_statement_candidate_id"), table_name="statement")
    op.drop_table("statement")
    op.drop_index(op.f("ix_event_candidate_extraction_id"), table_name="event_candidate")
    op.drop_index(op.f("ix_event_candidate_article_id"), table_name="event_candidate")
    op.drop_table("event_candidate")
    op.drop_index(op.f("ix_extraction_article_id"), table_name="extraction")
    op.drop_table("extraction")
