"""backtests: backtest_run and backtest_trade, never changed - a trigger refuses UPDATE, DELETE and TRUNCATE, so no
variant tried can be hidden -, and the default costs platform_settings.backtest_cost_bps and backtest_borrow_bps
(Prognoseprotokoll und Evaluation, story 2)

Revision ID: fa2483c61784
Revises: 2f7408122d7e
Create Date: 2026-11-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'fa2483c61784'
down_revision: str | Sequence[str] | None = '2f7408122d7e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A stored backtest run and its trades are never changed: deleting one would hide a variant tried (multiple testing).
# A DROP TABLE (this migration's downgrade) is not refused.
REFUSE = """
CREATE FUNCTION backtest_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'A backtest run cannot be changed once stored: % on % refused', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""
TABLES = ("backtest_run", "backtest_trade")


def upgrade() -> None:
    op.create_table('backtest_run',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('reproduces_id', sa.BigInteger(), nullable=True),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('code_version', sa.SmallInteger(), nullable=False),
    sa.Column('variant', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.Column('horizon', sa.SmallInteger(), nullable=False),
    sa.Column('min_probability', sa.String(length=10), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('prompt_version', sa.SmallInteger(), nullable=True),
    sa.Column('cost_bps', sa.Integer(), nullable=False),
    sa.Column('borrow_bps', sa.Integer(), nullable=False),
    sa.Column('training_until', sa.Date(), nullable=True),
    sa.Column('validation_until', sa.Date(), nullable=True),
    sa.Column('trades_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("min_probability IN ('high', 'medium', 'low')", name=op.f('ck_backtest_run_min_probability')),
    sa.CheckConstraint("mode IN ('created', 'event_day')", name=op.f('ck_backtest_run_mode')),
    sa.CheckConstraint('(model IS NULL) = (prompt_version IS NULL)', name=op.f('ck_backtest_run_model')),
    sa.CheckConstraint('borrow_bps BETWEEN 0 AND 5000', name=op.f('ck_backtest_run_borrow_bps')),
    sa.CheckConstraint('cost_bps BETWEEN 0 AND 500', name=op.f('ck_backtest_run_cost_bps')),
    sa.CheckConstraint('horizon IN (5, 20, 60)', name=op.f('ck_backtest_run_horizon')),
    sa.CheckConstraint('validation_until IS NULL OR (training_until IS NOT NULL AND validation_until > training_until)', name=op.f('ck_backtest_run_periods')),
    sa.ForeignKeyConstraint(['reproduces_id'], ['backtest_run.id'], name=op.f('fk_backtest_run_reproduces_id_backtest_run')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_run'))
    )
    op.create_index(op.f('ix_backtest_run_reproduces_id'), 'backtest_run', ['reproduces_id'], unique=False)
    op.create_index(op.f('ix_backtest_run_variant'), 'backtest_run', ['variant'], unique=False)
    op.create_table('backtest_trade',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('hypothesis_version_id', sa.BigInteger(), nullable=False),
    sa.Column('forecast_id', sa.BigInteger(), nullable=True),
    sa.Column('hypothesis_id', sa.BigInteger(), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=False),
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('benchmark_instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('probability', sa.String(length=10), nullable=False),
    sa.Column('signal_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('entry_day', sa.Date(), nullable=True),
    sa.Column('exit_day', sa.Date(), nullable=True),
    sa.Column('period', sa.String(length=12), nullable=False),
    sa.Column('cut', sa.Boolean(), nullable=False),
    sa.Column('trend', sa.String(length=5), nullable=True),
    sa.Column('volatility', sa.String(length=10), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('asset_return', sa.Float(), nullable=True),
    sa.Column('benchmark_return', sa.Float(), nullable=True),
    sa.Column('momentum', sa.Float(), nullable=True),
    sa.Column('random_direction', sa.String(length=10), nullable=False),
    sa.Column('missing', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.CheckConstraint("(status = 'measured') = (asset_return IS NOT NULL AND benchmark_return IS NOT NULL)", name=op.f('ck_backtest_trade_measured')),
    sa.CheckConstraint("direction IN ('positive', 'negative')", name=op.f('ck_backtest_trade_direction')),
    sa.CheckConstraint("kind = 'signal' OR forecast_id IS NOT NULL", name=op.f('ck_backtest_trade_paper')),
    sa.CheckConstraint("kind IN ('signal', 'paper')", name=op.f('ck_backtest_trade_kind')),
    sa.CheckConstraint("probability IN ('high', 'medium', 'low')", name=op.f('ck_backtest_trade_probability')),
    sa.CheckConstraint("random_direction IN ('positive', 'negative')", name=op.f('ck_backtest_trade_random_direction')),
    sa.CheckConstraint("status IN ('measured', 'gap', 'ended', 'open')", name=op.f('ck_backtest_trade_status')),
    sa.CheckConstraint("trend IN ('up', 'down')", name=op.f('ck_backtest_trade_trend')),
    sa.CheckConstraint("volatility IN ('calm', 'volatile')", name=op.f('ck_backtest_trade_volatility')),
    sa.ForeignKeyConstraint(['benchmark_instrument_id'], ['instrument.id'], name=op.f('fk_backtest_trade_benchmark_instrument_id_instrument')),
    sa.ForeignKeyConstraint(['event_id'], ['event.id'], name=op.f('fk_backtest_trade_event_id_event')),
    sa.ForeignKeyConstraint(['forecast_id'], ['forecast.id'], name=op.f('fk_backtest_trade_forecast_id_forecast')),
    sa.ForeignKeyConstraint(['hypothesis_id'], ['impact_hypothesis.id'], name=op.f('fk_backtest_trade_hypothesis_id_impact_hypothesis')),
    sa.ForeignKeyConstraint(['hypothesis_version_id'], ['impact_hypothesis_version.id'], name=op.f('fk_backtest_trade_hypothesis_version_id_impact_hypothesis_version')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_backtest_trade_instrument_id_instrument')),
    sa.ForeignKeyConstraint(['run_id'], ['backtest_run.id'], name=op.f('fk_backtest_trade_run_id_backtest_run')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_trade'))
    )
    op.create_index(op.f('ix_backtest_trade_run_id'), 'backtest_trade', ['run_id'], unique=False)
    op.add_column('platform_settings', sa.Column('backtest_cost_bps', sa.Integer(), server_default='10', nullable=False))
    op.add_column('platform_settings', sa.Column('backtest_borrow_bps', sa.Integer(), server_default='50', nullable=False))
    op.create_check_constraint(op.f("ck_platform_settings_backtest_cost_bps"), "platform_settings", "backtest_cost_bps BETWEEN 0 AND 500")
    op.create_check_constraint(op.f("ck_platform_settings_backtest_borrow_bps"), "platform_settings", "backtest_borrow_bps BETWEEN 0 AND 5000")
    op.execute(REFUSE)
    for table in TABLES:
        op.execute(f"CREATE TRIGGER {table}_no_change BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION backtest_immutable()")
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION backtest_immutable()")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_backtest_borrow_bps"), "platform_settings", type_="check")
    op.drop_constraint(op.f("ck_platform_settings_backtest_cost_bps"), "platform_settings", type_="check")
    op.drop_column('platform_settings', 'backtest_borrow_bps')
    op.drop_column('platform_settings', 'backtest_cost_bps')
    op.drop_index(op.f('ix_backtest_trade_run_id'), table_name='backtest_trade')
    op.drop_table('backtest_trade')
    op.drop_index(op.f('ix_backtest_run_variant'), table_name='backtest_run')
    op.drop_index(op.f('ix_backtest_run_reproduces_id'), table_name='backtest_run')
    op.drop_table('backtest_run')
    op.execute("DROP FUNCTION backtest_immutable()")
