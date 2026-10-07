"""add shift week receipt

Revision ID: f1b6c3e90d27
Revises: e9c4d2b70a13
Create Date: 2026-10-07

El acuse por trabajadora y semana: a quien se le mando el horario, si lo abrio y
si lo ha dado por bueno. `ShiftWeekPublication` guarda una fila por semana, que
sirve para saber si la semana esta cerrada pero no para saber si Maria se ha
enterado.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f1b6c3e90d27'
down_revision = 'e9c4d2b70a13'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'shift_week_receipt',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('week_start', sa.Date(), nullable=False),
        sa.Column('cleaner_id', sa.Integer(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('channel', sa.String(length=20), nullable=True),
        sa.Column('provider_id', sa.String(length=80), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('opened_at', sa.DateTime(), nullable=True),
        sa.Column('accepted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['cleaner_id'], ['cleaner.id'], name='fk_receipt_cleaner'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('week_start', 'cleaner_id', name='uq_receipt_semana'),
    )
    with op.batch_alter_table('shift_week_receipt', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shift_week_receipt_week_start'),
                              ['week_start'], unique=False)
        batch_op.create_index(batch_op.f('ix_shift_week_receipt_cleaner_id'),
                              ['cleaner_id'], unique=False)


def downgrade():
    with op.batch_alter_table('shift_week_receipt', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shift_week_receipt_cleaner_id'))
        batch_op.drop_index(batch_op.f('ix_shift_week_receipt_week_start'))
    op.drop_table('shift_week_receipt')
