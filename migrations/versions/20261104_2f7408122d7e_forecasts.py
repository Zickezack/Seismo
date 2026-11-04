"""forecasts: forecast (Prognose), never changed - a trigger refuses UPDATE, DELETE and TRUNCATE - and
forecast_outcome (Ergebnis) apart from it (Prognoseprotokoll und Evaluation, story 1)

Revision ID: 2f7408122d7e
Revises: b5ab8c26a4a3
Create Date: 2026-11-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2f7408122d7e"
down_revision: str | Sequence[str] | None = "b5ab8c26a4a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# datamodel.md, rule 12: a forecast cannot be changed once logged - not by the application, not by hand. A DROP TABLE
# (this migration's downgrade) is not refused.
REFUSE = """
CREATE FUNCTION forecast_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'A forecast cannot be changed once logged (datamodel.md, rule 12): % refused', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table('forecast',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('hypothesis_version_id', sa.BigInteger(), nullable=False),
    sa.Column('hypothesis_id', sa.BigInteger(), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=False),
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('benchmark_instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('benchmark', sa.String(length=10), nullable=False),
    sa.Column('sector_key', sa.String(length=40), nullable=True),
    sa.Column('horizon', sa.SmallInteger(), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('probability', sa.String(length=10), nullable=False),
    sa.Column('classification', sa.String(length=12), nullable=False),
    sa.Column('metric', sa.String(length=30), server_default='excess_return', nullable=False),
    sa.Column('entry_day', sa.Date(), nullable=False),
    sa.Column('end_day', sa.Date(), nullable=False),
    sa.Column('prices_through', sa.Date(), nullable=True),
    sa.Column('benchmark_prices_through', sa.Date(), nullable=True),
    sa.Column('origin', sa.String(length=10), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('prompt_version', sa.SmallInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("benchmark IN ('market', 'sector')", name=op.f('ck_forecast_benchmark')),
    sa.CheckConstraint("direction IN ('positive', 'negative')", name=op.f('ck_forecast_direction')),
    sa.CheckConstraint("origin IN ('llm', 'analyst')", name=op.f('ck_forecast_origin')),
    sa.CheckConstraint("probability IN ('high', 'medium', 'low')", name=op.f('ck_forecast_probability')),
    sa.CheckConstraint('end_day > entry_day', name=op.f('ck_forecast_period')),
    sa.CheckConstraint('horizon IN (5, 20, 60)', name=op.f('ck_forecast_horizon')),
    sa.ForeignKeyConstraint(['benchmark_instrument_id'], ['instrument.id'], name=op.f('fk_forecast_benchmark_instrument_id_instrument')),
    sa.ForeignKeyConstraint(['event_id'], ['event.id'], name=op.f('fk_forecast_event_id_event')),
    sa.ForeignKeyConstraint(['hypothesis_id'], ['impact_hypothesis.id'], name=op.f('fk_forecast_hypothesis_id_impact_hypothesis')),
    sa.ForeignKeyConstraint(['hypothesis_version_id'], ['impact_hypothesis_version.id'], name=op.f('fk_forecast_hypothesis_version_id_impact_hypothesis_version')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_forecast_instrument_id_instrument')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_forecast')),
    sa.UniqueConstraint('hypothesis_version_id', 'instrument_id', 'horizon', name=op.f('uq_forecast_hypothesis_version_id'))
    )
    op.create_index(op.f('ix_forecast_event_id'), 'forecast', ['event_id'], unique=False)
    op.create_index(op.f('ix_forecast_hypothesis_id'), 'forecast', ['hypothesis_id'], unique=False)
    op.create_index(op.f('ix_forecast_hypothesis_version_id'), 'forecast', ['hypothesis_version_id'], unique=False)
    op.create_index(op.f('ix_forecast_instrument_id'), 'forecast', ['instrument_id'], unique=False)
    op.create_table('forecast_outcome',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('forecast_id', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('asset_return', sa.Float(), nullable=True),
    sa.Column('benchmark_return', sa.Float(), nullable=True),
    sa.Column('excess_return', sa.Float(), nullable=True),
    sa.Column('hit', sa.Boolean(), nullable=True),
    sa.Column('missing', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('adjustments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'measured') = (excess_return IS NOT NULL)", name=op.f('ck_forecast_outcome_measured')),
    sa.CheckConstraint("status IN ('measured', 'gap', 'ended')", name=op.f('ck_forecast_outcome_status')),
    sa.ForeignKeyConstraint(['forecast_id'], ['forecast.id'], name=op.f('fk_forecast_outcome_forecast_id_forecast')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_forecast_outcome')),
    sa.UniqueConstraint('forecast_id', name=op.f('uq_forecast_outcome_forecast_id'))
    )
    op.execute(REFUSE)
    op.execute("CREATE TRIGGER forecast_no_change BEFORE UPDATE OR DELETE ON forecast FOR EACH ROW EXECUTE FUNCTION forecast_immutable()")
    op.execute("CREATE TRIGGER forecast_no_truncate BEFORE TRUNCATE ON forecast FOR EACH STATEMENT EXECUTE FUNCTION forecast_immutable()")


def downgrade() -> None:
    op.drop_table('forecast_outcome')
    op.execute("DROP TRIGGER forecast_no_truncate ON forecast")
    op.execute("DROP TRIGGER forecast_no_change ON forecast")
    op.drop_index(op.f('ix_forecast_instrument_id'), table_name='forecast')
    op.drop_index(op.f('ix_forecast_hypothesis_version_id'), table_name='forecast')
    op.drop_index(op.f('ix_forecast_hypothesis_id'), table_name='forecast')
    op.drop_index(op.f('ix_forecast_event_id'), table_name='forecast')
    op.drop_table('forecast')
    op.execute("DROP FUNCTION forecast_immutable()")
