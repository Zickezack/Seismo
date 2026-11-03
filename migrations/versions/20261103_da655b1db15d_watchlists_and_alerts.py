"""watchlists and alerts: watchlist and watchlist_item (Watchlist), alert_rule (Alert-Regel), alert (Alert) and
alert_hit (Treffer)

Revision ID: da655b1db15d
Revises: 55bbb214a90b
Create Date: 2026-11-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "da655b1db15d"
down_revision: str | Sequence[str] | None = "55bbb214a90b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('watchlist',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_watchlist'))
    )
    op.create_index('uq_watchlist_name', 'watchlist', [sa.literal_column('lower(name)')], unique=True)
    op.create_table('alert_rule',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('watchlist_id', sa.BigInteger(), nullable=True),
    sa.Column('criteria', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('on_new', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('on_change', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('on_report', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('max_per_hour', sa.Integer(), server_default='5', nullable=False),
    sa.Column('cooldown_hours', sa.Integer(), server_default='6', nullable=False),
    sa.Column('active_since', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('cooldown_hours BETWEEN 0 AND 168', name=op.f('ck_alert_rule_cooldown_hours')),
    sa.CheckConstraint('max_per_hour BETWEEN 1 AND 100', name=op.f('ck_alert_rule_max_per_hour')),
    sa.CheckConstraint('on_new OR on_change OR on_report', name=op.f('ck_alert_rule_trigger')),
    sa.ForeignKeyConstraint(['watchlist_id'], ['watchlist.id'], name=op.f('fk_alert_rule_watchlist_id_watchlist')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alert_rule'))
    )
    op.create_index(op.f('ix_alert_rule_watchlist_id'), 'alert_rule', ['watchlist_id'], unique=False)
    op.create_index('uq_alert_rule_name', 'alert_rule', [sa.literal_column('lower(name)')], unique=True)
    op.create_table('alert',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('rule_id', sa.BigInteger(), nullable=False),
    sa.Column('digest', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['rule_id'], ['alert_rule.id'], name=op.f('fk_alert_rule_id_alert_rule'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alert'))
    )
    op.create_index(op.f('ix_alert_rule_id'), 'alert', ['rule_id'], unique=False)
    op.create_index('ix_alert_unread', 'alert', ['created_at'], unique=False, postgresql_where=sa.text('read_at IS NULL'))
    op.create_table('watchlist_item',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('watchlist_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('entity_id', sa.BigInteger(), nullable=True),
    sa.Column('event_id', sa.BigInteger(), nullable=True),
    sa.Column('instrument_id', sa.BigInteger(), nullable=True),
    sa.Column('value', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('entity', 'event_type', 'country', 'event', 'instrument')", name=op.f('ck_watchlist_item_kind')),
    sa.CheckConstraint("num_nonnulls(entity_id, event_id, instrument_id, value) = 1 AND CASE kind WHEN 'entity' THEN entity_id IS NOT NULL WHEN 'event' THEN event_id IS NOT NULL WHEN 'instrument' THEN instrument_id IS NOT NULL ELSE value IS NOT NULL END", name=op.f('ck_watchlist_item_target')),
    sa.ForeignKeyConstraint(['entity_id'], ['entity.id'], name=op.f('fk_watchlist_item_entity_id_entity'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['event_id'], ['event.id'], name=op.f('fk_watchlist_item_event_id_event'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_watchlist_item_instrument_id_instrument'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['watchlist_id'], ['watchlist.id'], name=op.f('fk_watchlist_item_watchlist_id_watchlist'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_watchlist_item'))
    )
    op.create_index(op.f('ix_watchlist_item_entity_id'), 'watchlist_item', ['entity_id'], unique=False)
    op.create_index(op.f('ix_watchlist_item_event_id'), 'watchlist_item', ['event_id'], unique=False)
    op.create_index(op.f('ix_watchlist_item_instrument_id'), 'watchlist_item', ['instrument_id'], unique=False)
    op.create_index('uq_watchlist_item_target', 'watchlist_item', ['watchlist_id', 'kind', sa.literal_column('coalesce(value, CAST(coalesce(entity_id, event_id, instrument_id) AS TEXT))')], unique=True)
    op.create_table('alert_hit',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('rule_id', sa.BigInteger(), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=False),
    sa.Column('trigger', sa.String(length=10), nullable=False),
    sa.Column('state_id', sa.BigInteger(), nullable=True),
    sa.Column('candidate_id', sa.BigInteger(), nullable=True),
    sa.Column('detected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('alert_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint("CASE trigger WHEN 'change' THEN state_id IS NOT NULL WHEN 'report' THEN candidate_id IS NOT NULL ELSE true END", name=op.f('ck_alert_hit_target')),
    sa.CheckConstraint("trigger IN ('new', 'change', 'report')", name=op.f('ck_alert_hit_trigger')),
    sa.ForeignKeyConstraint(['alert_id'], ['alert.id'], name=op.f('fk_alert_hit_alert_id_alert'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['candidate_id'], ['event_candidate.id'], name=op.f('fk_alert_hit_candidate_id_event_candidate'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['event_id'], ['event.id'], name=op.f('fk_alert_hit_event_id_event'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_id'], ['alert_rule.id'], name=op.f('fk_alert_hit_rule_id_alert_rule'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['state_id'], ['event_state.id'], name=op.f('fk_alert_hit_state_id_event_state'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alert_hit'))
    )
    op.create_index(op.f('ix_alert_hit_alert_id'), 'alert_hit', ['alert_id'], unique=False)
    op.create_index(op.f('ix_alert_hit_candidate_id'), 'alert_hit', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_alert_hit_event_id'), 'alert_hit', ['event_id'], unique=False)
    op.create_index('ix_alert_hit_rule_id_event_id', 'alert_hit', ['rule_id', 'event_id'], unique=False)
    op.create_index(op.f('ix_alert_hit_state_id'), 'alert_hit', ['state_id'], unique=False)
    op.create_index('ix_alert_hit_waiting', 'alert_hit', ['rule_id'], unique=False, postgresql_where=sa.text('alert_id IS NULL'))
    op.create_index('uq_alert_hit_change', 'alert_hit', ['rule_id', 'state_id'], unique=True, postgresql_where=sa.text("trigger = 'change'"))
    op.create_index('uq_alert_hit_new', 'alert_hit', ['rule_id', 'event_id'], unique=True, postgresql_where=sa.text("trigger = 'new'"))
    op.create_index('uq_alert_hit_report', 'alert_hit', ['rule_id', 'candidate_id'], unique=True, postgresql_where=sa.text("trigger = 'report'"))


def downgrade() -> None:
    op.drop_index('uq_alert_hit_report', table_name='alert_hit', postgresql_where=sa.text("trigger = 'report'"))
    op.drop_index('uq_alert_hit_new', table_name='alert_hit', postgresql_where=sa.text("trigger = 'new'"))
    op.drop_index('uq_alert_hit_change', table_name='alert_hit', postgresql_where=sa.text("trigger = 'change'"))
    op.drop_index('ix_alert_hit_waiting', table_name='alert_hit', postgresql_where=sa.text('alert_id IS NULL'))
    op.drop_index(op.f('ix_alert_hit_state_id'), table_name='alert_hit')
    op.drop_index('ix_alert_hit_rule_id_event_id', table_name='alert_hit')
    op.drop_index(op.f('ix_alert_hit_event_id'), table_name='alert_hit')
    op.drop_index(op.f('ix_alert_hit_candidate_id'), table_name='alert_hit')
    op.drop_index(op.f('ix_alert_hit_alert_id'), table_name='alert_hit')
    op.drop_table('alert_hit')
    op.drop_index('uq_watchlist_item_target', table_name='watchlist_item')
    op.drop_index(op.f('ix_watchlist_item_instrument_id'), table_name='watchlist_item')
    op.drop_index(op.f('ix_watchlist_item_event_id'), table_name='watchlist_item')
    op.drop_index(op.f('ix_watchlist_item_entity_id'), table_name='watchlist_item')
    op.drop_table('watchlist_item')
    op.drop_index('ix_alert_unread', table_name='alert', postgresql_where=sa.text('read_at IS NULL'))
    op.drop_index(op.f('ix_alert_rule_id'), table_name='alert')
    op.drop_table('alert')
    op.drop_index('uq_alert_rule_name', table_name='alert_rule')
    op.drop_index(op.f('ix_alert_rule_watchlist_id'), table_name='alert_rule')
    op.drop_table('alert_rule')
    op.drop_index('uq_watchlist_name', table_name='watchlist')
    op.drop_table('watchlist')
