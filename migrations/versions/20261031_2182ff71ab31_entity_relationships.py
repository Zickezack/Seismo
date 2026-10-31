"""entity relationships: entity_relationship (Beziehung) and relationship_record (Angabe), its append-only history;
entity_recognition.relationships

Revision ID: 2182ff71ab31
Revises: 6544d7bab59f
Create Date: 2026-10-31

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2182ff71ab31"
down_revision: str | Sequence[str] | None = "6544d7bab59f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_relationship",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("object_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('supplies', 'competes', 'regulates', 'depends_on')", name=op.f("ck_entity_relationship_kind")),
        sa.CheckConstraint("subject_id != object_id", name=op.f("ck_entity_relationship_two_entities")),
        sa.ForeignKeyConstraint(["object_id"], ["entity.id"], name=op.f("fk_entity_relationship_object_id_entity"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["entity.id"], name=op.f("fk_entity_relationship_subject_id_entity"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_relationship")),
        sa.UniqueConstraint("kind", "subject_id", "object_id", name=op.f("uq_entity_relationship_kind")),
    )
    op.create_index(op.f("ix_entity_relationship_object_id"), "entity_relationship", ["object_id"])
    op.create_table(
        "relationship_record",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("relationship_id", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.String(length=10), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("statement_id", sa.BigInteger(), nullable=True),
        sa.Column("subject_alias_id", sa.BigInteger(), nullable=True),
        sa.Column("object_alias_id", sa.BigInteger(), nullable=True),
        sa.Column("reported_on", sa.Date(), nullable=True),
        sa.Column("ended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("review", sa.String(length=20), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("origin IN ('report', 'analyst')", name=op.f("ck_relationship_record_origin")),
        sa.CheckConstraint("review IN ('unreviewed', 'confirmed', 'disputed')", name=op.f("ck_relationship_record_review")),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["event_candidate.id"], name=op.f("fk_relationship_record_candidate_id_event_candidate"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["object_alias_id"], ["entity_alias.id"], name=op.f("fk_relationship_record_object_alias_id_entity_alias"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["relationship_id"], ["entity_relationship.id"],
            name=op.f("fk_relationship_record_relationship_id_entity_relationship"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"], ["statement.id"], name=op.f("fk_relationship_record_statement_id_statement"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["subject_alias_id"], ["entity_alias.id"], name=op.f("fk_relationship_record_subject_alias_id_entity_alias"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationship_record")),
    )
    op.create_index(op.f("ix_relationship_record_candidate_id"), "relationship_record", ["candidate_id"])
    op.create_index(op.f("ix_relationship_record_object_alias_id"), "relationship_record", ["object_alias_id"])
    op.create_index(op.f("ix_relationship_record_relationship_id"), "relationship_record", ["relationship_id"])
    op.create_index(op.f("ix_relationship_record_subject_alias_id"), "relationship_record", ["subject_alias_id"])
    op.add_column("entity_recognition", sa.Column("relationships", sa.Integer(), server_default=sa.text("0"), nullable=False))


def downgrade() -> None:
    op.drop_column("entity_recognition", "relationships")
    op.drop_index(op.f("ix_relationship_record_subject_alias_id"), table_name="relationship_record")
    op.drop_index(op.f("ix_relationship_record_relationship_id"), table_name="relationship_record")
    op.drop_index(op.f("ix_relationship_record_object_alias_id"), table_name="relationship_record")
    op.drop_index(op.f("ix_relationship_record_candidate_id"), table_name="relationship_record")
    op.drop_table("relationship_record")
    op.drop_index(op.f("ix_entity_relationship_object_id"), table_name="entity_relationship")
    op.drop_table("entity_relationship")
