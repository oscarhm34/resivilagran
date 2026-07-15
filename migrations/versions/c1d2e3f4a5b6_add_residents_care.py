"""add residents and care records

Revision ID: c1d2e3f4a5b6
Revises: a3f1b2c4d5e6
Create Date: 2026-04-25

"""
from alembic import op
import sqlalchemy as sa

revision = 'c1d2e3f4a5b6'
down_revision = 'a3f1b2c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'resident',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('nfc_code', sa.String(100), nullable=False),
        sa.Column('room_number', sa.String(10), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='1'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('nfc_code'),
    )

    op.create_table(
        'care_type',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table(
        'care_record',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=False),
        sa.Column('resident_id', sa.Integer(), nullable=False),
        sa.Column('care_type_id', sa.Integer(), nullable=True),
        sa.Column('start_time', sa.DateTime(), nullable=False),
        sa.Column('end_time', sa.DateTime(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['worker_id'], ['cleaner.id']),
        sa.ForeignKeyConstraint(['resident_id'], ['resident.id']),
        sa.ForeignKeyConstraint(['care_type_id'], ['care_type.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('care_record')
    op.drop_table('care_type')
    op.drop_table('resident')
