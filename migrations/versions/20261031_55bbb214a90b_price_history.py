"""price history: a Wertpapier's source and its id there, status, live and history flags; corporate_action
(Kapitalmassnahme), trading_day (Handelstag), market_data_sync; daily Kurspunkte; the interval and the years of history
in the Plattform-Einstellungen

Revision ID: 55bbb214a90b
Revises: a896bb40ab2d
Create Date: 2026-10-31

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "55bbb214a90b"
down_revision: str | Sequence[str] | None = "a896bb40ab2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("instrument", "is_active", new_column_name="live")
    op.add_column("instrument", sa.Column("source", sa.String(length=20), nullable=True))
    op.add_column("instrument", sa.Column("source_ref", sa.String(length=100), nullable=True))
    # The Wertpapiere so far came from EODHD, named by their code there.
    op.execute("UPDATE instrument SET source = 'eodhd', source_ref = symbol || '.' || exchange")
    op.alter_column("instrument", "source", nullable=False)
    op.alter_column("instrument", "source_ref", nullable=False)
    op.add_column("instrument", sa.Column("cusip", sa.String(length=9), nullable=True))
    op.add_column("instrument", sa.Column("entity_uncertain", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("instrument", sa.Column("status", sa.String(length=10), server_default="active", nullable=False))
    op.add_column("instrument", sa.Column("delisted_on", sa.Date(), nullable=True))
    op.add_column("instrument", sa.Column("history", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("instrument", sa.Column("history_through", sa.Date(), nullable=True))
    op.drop_constraint(op.f("uq_instrument_symbol"), "instrument", type_="unique")
    op.create_unique_constraint(op.f("uq_instrument_source"), "instrument", ["source", "source_ref"])
    op.create_check_constraint(op.f("ck_instrument_source"), "instrument", "source IN ('alpaca', 'six', 'eodhd')")
    op.create_check_constraint(op.f("ck_instrument_status"), "instrument", "status IN ('active', 'inactive')")
    op.create_index(op.f("ix_instrument_live"), "instrument", ["live"])
    op.create_index("ix_instrument_symbol", "instrument", ["source", "symbol"])
    op.drop_constraint(op.f("ck_price_point_resolution"), "price_point", type_="check")
    op.create_check_constraint(op.f("ck_price_point_resolution"), "price_point", "resolution IN ('quote', 'day')")
    op.create_table(
        "corporate_action",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("source_ref", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("process_date", sa.Date(), nullable=True),
        sa.Column("old_rate", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column("new_rate", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column("rate", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column("old_symbol", sa.String(length=30), nullable=True),
        sa.Column("new_symbol", sa.String(length=30), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name=op.f("fk_corporate_action_instrument_id_instrument"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corporate_action")),
        sa.UniqueConstraint("source", "source_ref", "instrument_id", name=op.f("uq_corporate_action_source")),
    )
    op.create_index(op.f("ix_corporate_action_instrument_id"), "corporate_action", ["instrument_id"])
    op.create_table(
        "trading_day",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_trading_day")),
    )
    op.create_table(
        "market_data_sync",
        sa.Column("name", sa.String(length=30), nullable=False),
        sa.Column("through", sa.Date(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_market_data_sync")),
    )
    op.add_column("platform_settings", sa.Column("price_interval_minutes", sa.Integer(), server_default="30", nullable=False))
    op.add_column("platform_settings", sa.Column("price_history_years", sa.Integer(), server_default="2", nullable=False))
    op.create_check_constraint(
        op.f("ck_platform_settings_price_interval_minutes"), "platform_settings", "price_interval_minutes BETWEEN 5 AND 1440"
    )
    op.create_check_constraint(
        op.f("ck_platform_settings_price_history_years"), "platform_settings", "price_history_years BETWEEN 1 AND 30"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_price_history_years"), "platform_settings", type_="check")
    op.drop_constraint(op.f("ck_platform_settings_price_interval_minutes"), "platform_settings", type_="check")
    op.drop_column("platform_settings", "price_history_years")
    op.drop_column("platform_settings", "price_interval_minutes")
    op.drop_table("market_data_sync")
    op.drop_table("trading_day")
    op.drop_index(op.f("ix_corporate_action_instrument_id"), table_name="corporate_action")
    op.drop_table("corporate_action")
    # The daily prices and the Wertpapiere of other sources than EODHD go: the code before knows neither.
    op.execute("DELETE FROM price_point WHERE resolution = 'day'")
    op.execute("DELETE FROM instrument WHERE source <> 'eodhd'")
    op.drop_constraint(op.f("ck_price_point_resolution"), "price_point", type_="check")
    op.create_check_constraint(op.f("ck_price_point_resolution"), "price_point", "resolution IN ('quote')")
    op.drop_index("ix_instrument_symbol", table_name="instrument")
    op.drop_index(op.f("ix_instrument_live"), table_name="instrument")
    op.drop_constraint(op.f("ck_instrument_status"), "instrument", type_="check")
    op.drop_constraint(op.f("ck_instrument_source"), "instrument", type_="check")
    op.drop_constraint(op.f("uq_instrument_source"), "instrument", type_="unique")
    op.create_unique_constraint(op.f("uq_instrument_symbol"), "instrument", ["symbol", "exchange"])
    for column in ("history_through", "history", "delisted_on", "status", "entity_uncertain", "cusip", "source_ref", "source"):
        op.drop_column("instrument", column)
    op.alter_column("instrument", "live", new_column_name="is_active")
