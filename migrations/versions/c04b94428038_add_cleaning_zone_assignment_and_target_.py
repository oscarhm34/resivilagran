"""add cleaning zone assignment and target time

Revision ID: c04b94428038
Revises: f5dc25be443b
Create Date: 2026-08-07 15:05:04.896115

"""
from alembic import op
import sqlalchemy as sa

revision = 'c04b94428038'
down_revision = 'f5dc25be443b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cleaning_target_time',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('room_type_id', sa.Integer(), nullable=False),
    sa.Column('target_minutes', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['room_type_id'], ['room_type.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('room_type_id')
    )
    op.create_table('cleaning_zone_assignment',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cleaner_id', sa.Integer(), nullable=False),
    sa.Column('floor_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['cleaner_id'], ['cleaner.id'], ),
    sa.ForeignKeyConstraint(['floor_id'], ['floor.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cleaner_id', 'floor_id', name='uq_cleaner_floor')
    )


def downgrade():
    op.drop_table('cleaning_zone_assignment')
    op.drop_table('cleaning_target_time')
