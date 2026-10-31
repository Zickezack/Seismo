"""reprocessing: reprocessing (Neuverarbeitung) and storage_measurement (Speichermessung); extraction.replaced_by_id
and reprocessing_id, entity_recognition.reprocessing_id, relationship_record.replaced_at, the correction kind
reprocess, platform_settings.reprocessings_per_hour

Revision ID: fb4c52ab16d6
Revises: 2182ff71ab31
Create Date: 2026-10-31

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fb4c52ab16d6"
down_revision: str | Sequence[str] | None = "2182ff71ab31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reprocessing",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("step", sa.String(length=20), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("source_name", sa.String(length=200), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("step IN ('extraction', 'recognition')", name=op.f("ck_reprocessing_step")),
        sa.CheckConstraint("status IN ('running', 'done', 'cancelled')", name=op.f("ck_reprocessing_status")),
        sa.CheckConstraint("period_start <= period_end", name=op.f("ck_reprocessing_period")),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], name=op.f("fk_reprocessing_source_id_source"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reprocessing")),
    )
    op.create_table(
        "storage_measurement",
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("database_bytes", sa.BigInteger(), nullable=False),
        sa.Column("table_bytes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("article_count", sa.BigInteger(), nullable=False),
        sa.Column("raw_text_count", sa.BigInteger(), nullable=False),
        sa.Column("raw_text_bytes", sa.BigInteger(), nullable=False),
        sa.Column("backup_count", sa.Integer(), nullable=False),
        sa.Column("backup_bytes", sa.BigInteger(), nullable=False),
        sa.Column("backup_free_bytes", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("measured_on", name=op.f("pk_storage_measurement")),
    )
    op.add_column("extraction", sa.Column("replaced_by_id", sa.BigInteger(), nullable=True))
    op.add_column("extraction", sa.Column("reprocessing_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_extraction_replaced_by_id"), "extraction", ["replaced_by_id"])
    op.create_index(op.f("ix_extraction_reprocessing_id"), "extraction", ["reprocessing_id"])
    op.create_foreign_key(
        op.f("fk_extraction_replaced_by_id_extraction"), "extraction", "extraction", ["replaced_by_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        op.f("fk_extraction_reprocessing_id_reprocessing"), "extraction", "reprocessing", ["reprocessing_id"], ["id"],
        ondelete="SET NULL",
    )
    op.add_column("entity_recognition", sa.Column("reprocessing_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_entity_recognition_reprocessing_id"), "entity_recognition", ["reprocessing_id"])
    op.create_foreign_key(
        op.f("fk_entity_recognition_reprocessing_id_reprocessing"), "entity_recognition", "reprocessing", ["reprocessing_id"],
        ["id"], ondelete="SET NULL",
    )
    op.add_column("relationship_record", sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_event_correction_kind"), "event_correction", type_="check")
    op.create_check_constraint(op.f("ck_event_correction_kind"), "event_correction", "kind IN ('merge', 'split', 'reprocess')")
    op.add_column("platform_settings", sa.Column("reprocessings_per_hour", sa.Integer(), server_default="30", nullable=False))
    op.create_check_constraint(op.f("ck_platform_settings_reprocessings_per_hour"), "platform_settings", "reprocessings_per_hour >= 0")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_reprocessings_per_hour"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "reprocessings_per_hour")
    # As if every article had only ever been read the new way: the versions the replaced reports stated go with them,
    # those a reprocessing appended stay as their reports' own; then the replaced readings and the corrections.
    op.execute("""
        DELETE FROM event_state
        WHERE withdrawn_by_id IN (SELECT id FROM event_correction WHERE kind = 'reprocess')
    """)
    op.execute("""
        UPDATE event_state SET added_by_id = NULL
        WHERE added_by_id IN (SELECT id FROM event_correction WHERE kind = 'reprocess')
    """)
    op.execute("DELETE FROM event_correction WHERE kind = 'reprocess'")
    op.execute("DELETE FROM extraction WHERE replaced_by_id IS NOT NULL")
    op.drop_constraint(op.f("ck_event_correction_kind"), "event_correction", type_="check")
    op.create_check_constraint(op.f("ck_event_correction_kind"), "event_correction", "kind IN ('merge', 'split')")
    # A replaced Angabe counted no more; without the mark it would again.
    op.execute("DELETE FROM relationship_record WHERE replaced_at IS NOT NULL")
    op.drop_column("relationship_record", "replaced_at")
    op.drop_constraint(op.f("fk_entity_recognition_reprocessing_id_reprocessing"), "entity_recognition", type_="foreignkey")
    op.drop_index(op.f("ix_entity_recognition_reprocessing_id"), table_name="entity_recognition")
    op.drop_column("entity_recognition", "reprocessing_id")
    op.drop_constraint(op.f("fk_extraction_reprocessing_id_reprocessing"), "extraction", type_="foreignkey")
    op.drop_constraint(op.f("fk_extraction_replaced_by_id_extraction"), "extraction", type_="foreignkey")
    op.drop_index(op.f("ix_extraction_reprocessing_id"), table_name="extraction")
    op.drop_index(op.f("ix_extraction_replaced_by_id"), table_name="extraction")
    op.drop_column("extraction", "reprocessing_id")
    op.drop_column("extraction", "replaced_by_id")
    op.drop_table("storage_measurement")
    op.drop_table("reprocessing")
