"""entities: entity (Entität), entity_alias (Namensform), entity_mention (Nennung), entity_recognition
(Entitätenerkennung); the extension pg_trgm for similar names

Revision ID: 6544d7bab59f
Revises: 0a882e94c29f
Create Date: 2026-10-31

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6544d7bab59f"
down_revision: str | Sequence[str] | None = "0a882e94c29f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Trusted since PostgreSQL 13: the database's owner may create it, no superuser needed.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "entity",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("merged_into_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('company', 'subsidiary', 'person', 'country', 'authority', 'organization', 'product', 'commodity', 'sector')",
            name=op.f("ck_entity_kind"),
        ),
        sa.ForeignKeyConstraint(["merged_into_id"], ["entity.id"], name=op.f("fk_entity_merged_into_id_entity"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_id"], ["entity.id"], name=op.f("fk_entity_parent_id_entity"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity")),
    )
    op.create_index(op.f("ix_entity_parent_id"), "entity", ["parent_id"])
    op.create_table(
        "entity_alias",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column("scheme", sa.String(length=20), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("match", sa.String(length=20), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("uncertain", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("match IN ('founding', 'name', 'identifier', 'llm', 'seed', 'split')", name=op.f("ck_entity_alias_match")),
        sa.CheckConstraint("scheme IN ('name', 'iso3166', 'isin', 'lei', 'ticker', 'catalogue')", name=op.f("ck_entity_alias_scheme")),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], name=op.f("fk_entity_alias_entity_id_entity"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_alias")),
    )
    op.create_index(op.f("ix_entity_alias_entity_id"), "entity_alias", ["entity_id"])
    op.create_index("ix_entity_alias_scheme_key", "entity_alias", ["scheme", "key"])
    op.create_index(
        "uq_entity_alias_identifier", "entity_alias", ["scheme", "key"], unique=True, postgresql_where=sa.text("scheme != 'name'")
    )
    op.create_index(
        "ix_entity_alias_key_trgm", "entity_alias", ["key"], postgresql_using="gin", postgresql_ops={"key": "gin_trgm_ops"},
        postgresql_where=sa.text("scheme = 'name'"),
    )
    op.create_table(
        "entity_mention",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("alias_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("source IN ('actor', 'impact')", name=op.f("ck_entity_mention_source")),
        sa.ForeignKeyConstraint(
            ["alias_id"], ["entity_alias.id"], name=op.f("fk_entity_mention_alias_id_entity_alias"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["event_candidate.id"], name=op.f("fk_entity_mention_candidate_id_event_candidate"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_mention")),
        sa.UniqueConstraint("candidate_id", "source", "position", name=op.f("uq_entity_mention_candidate_id")),
    )
    op.create_index(op.f("ix_entity_mention_alias_id"), "entity_mention", ["alias_id"])
    op.create_index(op.f("ix_entity_mention_candidate_id"), "entity_mention", ["candidate_id"])
    op.create_table(
        "entity_recognition",
        sa.Column("extraction_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("recognised_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("names", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("linked", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["extraction_id"], ["extraction.id"], name=op.f("fk_entity_recognition_extraction_id_extraction"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("extraction_id", name=op.f("pk_entity_recognition")),
    )


def downgrade() -> None:
    op.drop_table("entity_recognition")
    op.drop_index(op.f("ix_entity_mention_candidate_id"), table_name="entity_mention")
    op.drop_index(op.f("ix_entity_mention_alias_id"), table_name="entity_mention")
    op.drop_table("entity_mention")
    op.drop_index("ix_entity_alias_key_trgm", table_name="entity_alias")
    op.drop_index("uq_entity_alias_identifier", table_name="entity_alias")
    op.drop_index("ix_entity_alias_scheme_key", table_name="entity_alias")
    op.drop_index(op.f("ix_entity_alias_entity_id"), table_name="entity_alias")
    op.drop_table("entity_alias")
    op.drop_index(op.f("ix_entity_parent_id"), table_name="entity")
    op.drop_table("entity")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
