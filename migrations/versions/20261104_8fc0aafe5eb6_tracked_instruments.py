"""tracked instruments: instrument.tracked, tracked_since, collected_through and backfill_through; price_point
resolutions minute and hour with samples; price_gap (Erfassungslücke); platform_settings.price_full_resolution_days,
tracked_instruments_max and minute_backfill_days (Live-Kurse und Kursdatenspeicher, story 2)

Revision ID: 8fc0aafe5eb6
Revises: 3c7e9a1f5b20
Create Date: 2026-11-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8fc0aafe5eb6"
down_revision: str | Sequence[str] | None = "3c7e9a1f5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_gap",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cause", sa.String(length=20), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cause IN ('not_asked', 'source_error')", name=op.f("ck_price_gap_cause")),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_price_gap_period")),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name=op.f("fk_price_gap_instrument_id_instrument"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_gap")),
    )
    op.create_index(op.f("ix_price_gap_instrument_id"), "price_gap", ["instrument_id"], unique=False)
    op.add_column("instrument", sa.Column("tracked", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("instrument", sa.Column("tracked_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("instrument", sa.Column("collected_through", sa.DateTime(timezone=True), nullable=True))
    op.add_column("instrument", sa.Column("backfill_through", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_instrument_tracked", "instrument", ["tracked"], unique=False)
    op.add_column("platform_settings", sa.Column("price_full_resolution_days", sa.Integer(), server_default="90", nullable=False))
    op.add_column("platform_settings", sa.Column("tracked_instruments_max", sa.Integer(), server_default="50", nullable=False))
    op.create_check_constraint(
        op.f("ck_platform_settings_price_full_resolution_days"), "platform_settings", "price_full_resolution_days BETWEEN 0 AND 3650"
    )
    op.create_check_constraint(
        op.f("ck_platform_settings_tracked_instruments_max"), "platform_settings", "tracked_instruments_max BETWEEN 0 AND 1000"
    )
    op.add_column("platform_settings", sa.Column("minute_backfill_days", sa.Integer(), server_default="90", nullable=False))
    op.create_check_constraint(
        op.f("ck_platform_settings_minute_backfill_days"), "platform_settings", "minute_backfill_days BETWEEN 0 AND 3650"
    )
    op.add_column("price_point", sa.Column("samples", sa.Integer(), nullable=True))
    op.drop_constraint(op.f("ck_price_point_resolution"), "price_point", type_="check")
    op.create_check_constraint(op.f("ck_price_point_resolution"), "price_point", "resolution IN ('quote', 'minute', 'hour', 'day')")


def downgrade() -> None:
    # The code before knows neither minutes nor hours: they go, with what an hour replaced (as if never tracked).
    op.execute("DELETE FROM price_point WHERE resolution IN ('minute', 'hour')")
    op.drop_constraint(op.f("ck_price_point_resolution"), "price_point", type_="check")
    op.create_check_constraint(op.f("ck_price_point_resolution"), "price_point", "resolution IN ('quote', 'day')")
    op.drop_column("price_point", "samples")
    op.drop_constraint(op.f("ck_platform_settings_minute_backfill_days"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "minute_backfill_days")
    op.drop_constraint(op.f("ck_platform_settings_tracked_instruments_max"), "platform_settings", type_="check")
    op.drop_constraint(op.f("ck_platform_settings_price_full_resolution_days"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "tracked_instruments_max")
    op.drop_column("platform_settings", "price_full_resolution_days")
    op.drop_index("ix_instrument_tracked", table_name="instrument")
    op.drop_column("instrument", "backfill_through")
    op.drop_column("instrument", "collected_through")
    op.drop_column("instrument", "tracked_since")
    op.drop_column("instrument", "tracked")
    op.drop_index(op.f("ix_price_gap_instrument_id"), table_name="price_gap")
    op.drop_table("price_gap")
