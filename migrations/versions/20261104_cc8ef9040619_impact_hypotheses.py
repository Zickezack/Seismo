"""impact hypotheses: impact_assessment, impact_hypothesis and its versions, platform_settings.impact_llm_calls_per_hour
(Markt- und Wirkungsmodell, story 1)

Revision ID: cc8ef9040619
Revises: 5b22c0023be1
Create Date: 2026-11-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "cc8ef9040619"
down_revision: str | Sequence[str] | None = "5b22c0023be1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('impact_assessment',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('prompt_version', sa.SmallInteger(), nullable=False),
    sa.Column('reports', sa.Integer(), nullable=False),
    sa.Column('hypotheses', sa.Integer(), nullable=False),
    sa.Column('dropped', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['event.id'], name=op.f('fk_impact_assessment_event_id_event'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_impact_assessment'))
    )
    op.create_index(op.f('ix_impact_assessment_event_id'), 'impact_assessment', ['event_id'], unique=False)
    op.create_table('impact_hypothesis',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=False),
    sa.Column('target_entity_id', sa.BigInteger(), nullable=False),
    sa.Column('path', sa.String(length=10), nullable=False),
    sa.Column('via_relationship_id', sa.BigInteger(), nullable=True),
    sa.Column('via_entity_id', sa.BigInteger(), nullable=True),
    sa.Column('origin', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(path = 'direct') = (via_entity_id IS NULL)", name=op.f('ck_impact_hypothesis_via')),
    sa.CheckConstraint("origin IN ('llm', 'analyst')", name=op.f('ck_impact_hypothesis_origin')),
    sa.CheckConstraint("path IN ('direct', 'indirect')", name=op.f('ck_impact_hypothesis_path')),
    sa.ForeignKeyConstraint(['event_id'], ['event.id'], name=op.f('fk_impact_hypothesis_event_id_event'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_entity_id'], ['entity.id'], name=op.f('fk_impact_hypothesis_target_entity_id_entity')),
    sa.ForeignKeyConstraint(['via_entity_id'], ['entity.id'], name=op.f('fk_impact_hypothesis_via_entity_id_entity')),
    sa.ForeignKeyConstraint(['via_relationship_id'], ['entity_relationship.id'], name=op.f('fk_impact_hypothesis_via_relationship_id_entity_relationship'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_impact_hypothesis'))
    )
    op.create_index(op.f('ix_impact_hypothesis_event_id'), 'impact_hypothesis', ['event_id'], unique=False)
    op.create_index(op.f('ix_impact_hypothesis_target_entity_id'), 'impact_hypothesis', ['target_entity_id'], unique=False)
    op.create_table('impact_hypothesis_version',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('hypothesis_id', sa.BigInteger(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('horizon', sa.String(length=10), nullable=False),
    sa.Column('mechanism', sa.Text(), nullable=False),
    sa.Column('counterarguments', sa.Text(), nullable=False),
    sa.Column('classification', sa.String(length=12), nullable=False),
    sa.Column('proposed_classification', sa.String(length=12), nullable=True),
    sa.Column('support', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('against', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('origin', sa.String(length=10), nullable=False),
    sa.Column('assessment_id', sa.BigInteger(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("classification IN ('evidenced', 'plausible', 'speculative')", name=op.f('ck_impact_hypothesis_version_classification')),
    sa.CheckConstraint("direction IN ('positive', 'negative', 'mixed', 'unclear')", name=op.f('ck_impact_hypothesis_version_direction')),
    sa.CheckConstraint("horizon IN ('days', 'weeks', 'months', 'years', 'unknown')", name=op.f('ck_impact_hypothesis_version_horizon')),
    sa.CheckConstraint("origin IN ('llm', 'analyst')", name=op.f('ck_impact_hypothesis_version_origin')),
    sa.CheckConstraint("status IN ('proposed', 'confirmed', 'rejected', 'superseded')", name=op.f('ck_impact_hypothesis_version_status')),
    sa.ForeignKeyConstraint(['assessment_id'], ['impact_assessment.id'], name=op.f('fk_impact_hypothesis_version_assessment_id_impact_assessment'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['hypothesis_id'], ['impact_hypothesis.id'], name=op.f('fk_impact_hypothesis_version_hypothesis_id_impact_hypothesis'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_impact_hypothesis_version')),
    sa.UniqueConstraint('hypothesis_id', 'number', name=op.f('uq_impact_hypothesis_version_hypothesis_id'))
    )
    op.add_column('platform_settings', sa.Column('impact_llm_calls_per_hour', sa.Integer(), server_default='20', nullable=False))
    op.create_check_constraint(
        op.f("ck_platform_settings_impact_llm_calls_per_hour"), "platform_settings", "impact_llm_calls_per_hour BETWEEN 0 AND 1000"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_impact_llm_calls_per_hour"), "platform_settings", type_="check")
    op.drop_column('platform_settings', 'impact_llm_calls_per_hour')
    op.drop_table('impact_hypothesis_version')
    op.drop_index(op.f('ix_impact_hypothesis_target_entity_id'), table_name='impact_hypothesis')
    op.drop_index(op.f('ix_impact_hypothesis_event_id'), table_name='impact_hypothesis')
    op.drop_table('impact_hypothesis')
    op.drop_index(op.f('ix_impact_assessment_event_id'), table_name='impact_assessment')
    op.drop_table('impact_assessment')
