"""status page: llm_call (every LLM call of a job) and job_hour (what each job kind did per hour), and
platform_settings.llm_cost_rappen_per_hour (Skalierung und Betriebsqualität, story 1)

Revision ID: be32b170af1a
Revises: fa2483c61784
Create Date: 2026-11-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'be32b170af1a'
down_revision: str | Sequence[str] | None = 'fa2483c61784'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('job_hour',
    sa.Column('hour', sa.DateTime(timezone=True), nullable=False),
    sa.Column('kind', sa.String(length=100), nullable=False),
    sa.Column('finished', sa.Integer(), nullable=False),
    sa.Column('failed', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('failed_attempts', sa.Integer(), nullable=False),
    sa.Column('started', sa.Integer(), nullable=False),
    sa.Column('wait_seconds', sa.Float(), nullable=False),
    sa.Column('wait_seconds_max', sa.Float(), nullable=False),
    sa.Column('run_seconds', sa.Float(), nullable=False),
    sa.Column('run_seconds_max', sa.Float(), nullable=False),
    sa.Column('queued_due', sa.Integer(), nullable=False),
    sa.Column('oldest_due_seconds', sa.Float(), nullable=True),
    sa.Column('measured_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('hour', 'kind', name=op.f('pk_job_hour'))
    )
    op.create_table('llm_call',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('called_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=100), nullable=False),
    sa.Column('job_id', sa.BigInteger(), nullable=True),
    sa.Column('article_id', sa.BigInteger(), nullable=True),
    sa.Column('model', sa.String(length=200), nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('answer_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('outcome', sa.String(length=12), nullable=False),
    sa.CheckConstraint("outcome IN ('answered', 'cut', 'timeout', 'unreachable', 'rejected')", name=op.f('ck_llm_call_outcome')),
    sa.CheckConstraint('duration_ms >= 0', name=op.f('ck_llm_call_duration_ms')),
    sa.ForeignKeyConstraint(['article_id'], ['article.id'], name=op.f('fk_llm_call_article_id_article'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_call'))
    )
    op.create_index(op.f('ix_llm_call_article_id'), 'llm_call', ['article_id'], unique=False)
    op.create_index(op.f('ix_llm_call_called_at'), 'llm_call', ['called_at'], unique=False)
    op.add_column('platform_settings', sa.Column('llm_cost_rappen_per_hour', sa.Integer(), server_default='0', nullable=False))
    op.create_check_constraint(
        op.f("ck_platform_settings_llm_cost_rappen_per_hour"), "platform_settings", "llm_cost_rappen_per_hour BETWEEN 0 AND 100000"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_settings_llm_cost_rappen_per_hour"), "platform_settings", type_="check")
    op.drop_column('platform_settings', 'llm_cost_rappen_per_hour')
    op.drop_index(op.f('ix_llm_call_called_at'), table_name='llm_call')
    op.drop_index(op.f('ix_llm_call_article_id'), table_name='llm_call')
    op.drop_table('llm_call')
    op.drop_table('job_hour')
