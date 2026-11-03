"""situations: situation (Lage), situation_member (Lage-Zuordnung) and situation_check; a Lage as a watchlist item;
platform_settings.situation_open_days and situation_llm_calls_per_hour

Revision ID: ff06b2d2f906
Revises: da655b1db15d
Create Date: 2026-11-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ff06b2d2f906"
down_revision: str | Sequence[str] | None = "da655b1db15d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('situation',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='proposed', nullable=False),
    sa.Column('merged_into_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('proposed', 'confirmed', 'rejected', 'dismissed', 'merged')", name=op.f('ck_situation_status')),
    sa.ForeignKeyConstraint(['merged_into_id'], ['situation.id'], name=op.f('fk_situation_merged_into_id_situation'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_situation'))
    )
    op.create_index('ix_situation_status', 'situation', ['status'], unique=False)
    op.create_table('situation_check',
    sa.Column('event_id', sa.BigInteger(), nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['event.id'], name=op.f('fk_situation_check_event_id_event'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('event_id', name=op.f('pk_situation_check'))
    )
    op.create_table('situation_member',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('situation_id', sa.BigInteger(), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('origin', sa.String(length=20), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('moved_to_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint("origin IN ('proposal', 'score', 'llm', 'analyst')", name=op.f('ck_situation_member_origin')),
    sa.CheckConstraint("status IN ('proposed', 'automatic', 'confirmed', 'removed')", name=op.f('ck_situation_member_status')),
    sa.ForeignKeyConstraint(['event_id'], ['event.id'], name=op.f('fk_situation_member_event_id_event'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['moved_to_id'], ['situation.id'], name=op.f('fk_situation_member_moved_to_id_situation'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['situation_id'], ['situation.id'], name=op.f('fk_situation_member_situation_id_situation'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_situation_member')),
    sa.UniqueConstraint('situation_id', 'event_id', name=op.f('uq_situation_member_situation_id'))
    )
    op.create_index(op.f('ix_situation_member_event_id'), 'situation_member', ['event_id'], unique=False)
    op.add_column('platform_settings', sa.Column('situation_open_days', sa.Integer(), server_default='30', nullable=False))
    op.add_column('platform_settings', sa.Column('situation_llm_calls_per_hour', sa.Integer(), server_default='20', nullable=False))
    op.add_column('watchlist_item', sa.Column('situation_id', sa.BigInteger(), nullable=True))
    op.drop_index(op.f('uq_watchlist_item_target'), table_name='watchlist_item')
    op.create_index('uq_watchlist_item_target', 'watchlist_item', ['watchlist_id', 'kind', sa.literal_column('coalesce(value, CAST(coalesce(entity_id, event_id, instrument_id, situation_id) AS TEXT))')], unique=True)
    op.create_index(op.f('ix_watchlist_item_situation_id'), 'watchlist_item', ['situation_id'], unique=False)
    op.create_foreign_key(op.f('fk_watchlist_item_situation_id_situation'), 'watchlist_item', 'situation', ['situation_id'], ['id'], ondelete='CASCADE')
    # Check constraints are not compared by autogenerate.
    op.drop_constraint(op.f("ck_watchlist_item_kind"), "watchlist_item", type_="check")
    op.create_check_constraint(
        op.f("ck_watchlist_item_kind"), "watchlist_item", "kind IN ('entity', 'event_type', 'country', 'event', 'instrument', 'situation')"
    )
    op.drop_constraint(op.f("ck_watchlist_item_target"), "watchlist_item", type_="check")
    op.create_check_constraint(
        op.f("ck_watchlist_item_target"), "watchlist_item",
        "num_nonnulls(entity_id, event_id, instrument_id, situation_id, value) = 1 AND CASE kind"
        " WHEN 'entity' THEN entity_id IS NOT NULL WHEN 'event' THEN event_id IS NOT NULL"
        " WHEN 'instrument' THEN instrument_id IS NOT NULL WHEN 'situation' THEN situation_id IS NOT NULL"
        " ELSE value IS NOT NULL END",
    )


def downgrade() -> None:
    # The code before knows no Lage as a watchlist item.
    op.execute("DELETE FROM watchlist_item WHERE kind = 'situation'")
    op.drop_constraint(op.f("ck_watchlist_item_target"), "watchlist_item", type_="check")
    op.create_check_constraint(
        op.f("ck_watchlist_item_target"), "watchlist_item",
        "num_nonnulls(entity_id, event_id, instrument_id, value) = 1 AND CASE kind"
        " WHEN 'entity' THEN entity_id IS NOT NULL WHEN 'event' THEN event_id IS NOT NULL"
        " WHEN 'instrument' THEN instrument_id IS NOT NULL ELSE value IS NOT NULL END",
    )
    op.drop_constraint(op.f("ck_watchlist_item_kind"), "watchlist_item", type_="check")
    op.create_check_constraint(
        op.f("ck_watchlist_item_kind"), "watchlist_item", "kind IN ('entity', 'event_type', 'country', 'event', 'instrument')"
    )
    op.drop_constraint(op.f('fk_watchlist_item_situation_id_situation'), 'watchlist_item', type_='foreignkey')
    op.drop_index(op.f('ix_watchlist_item_situation_id'), table_name='watchlist_item')
    op.drop_index('uq_watchlist_item_target', table_name='watchlist_item')
    op.create_index(op.f('uq_watchlist_item_target'), 'watchlist_item', ['watchlist_id', 'kind', sa.literal_column('coalesce(value, CAST(coalesce(entity_id, event_id, instrument_id) AS TEXT))')], unique=True)
    op.drop_column('watchlist_item', 'situation_id')
    op.drop_column('platform_settings', 'situation_llm_calls_per_hour')
    op.drop_column('platform_settings', 'situation_open_days')
    op.drop_index(op.f('ix_situation_member_event_id'), table_name='situation_member')
    op.drop_table('situation_member')
    op.drop_table('situation_check')
    op.drop_index('ix_situation_status', table_name='situation')
    op.drop_table('situation')
