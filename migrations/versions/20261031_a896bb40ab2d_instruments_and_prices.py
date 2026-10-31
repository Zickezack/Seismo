"""instruments and prices: instrument (Wertpapier) and price_point (Kurspunkt)

Revision ID: a896bb40ab2d
Revises: f58df57d5e12
Create Date: 2026-10-31

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a896bb40ab2d"
down_revision: str | Sequence[str] | None = "f58df57d5e12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("entity_id", sa.BigInteger(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('stock', 'etf', 'index')", name=op.f("ck_instrument_kind")),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], name=op.f("fk_instrument_entity_id_entity"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument")),
        sa.UniqueConstraint("symbol", "exchange", name=op.f("uq_instrument_symbol")),
    )
    op.create_index(op.f("ix_instrument_entity_id"), "instrument", ["entity_id"])
    op.create_table(
        "price_point",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("resolution", sa.String(length=10), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("high", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("low", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("previous_close", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("delayed", sa.Boolean(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("resolution IN ('quote')", name=op.f("ck_price_point_resolution")),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name=op.f("fk_price_point_instrument_id_instrument"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_point")),
        sa.UniqueConstraint("instrument_id", "resolution", "at", name=op.f("uq_price_point_instrument_id")),
    )


def downgrade() -> None:
    op.drop_table("price_point")
    op.drop_index(op.f("ix_instrument_entity_id"), table_name="instrument")
    op.drop_table("instrument")
