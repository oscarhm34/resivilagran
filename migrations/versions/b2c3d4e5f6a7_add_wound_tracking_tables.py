"""Add wound tracking tables

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-13

"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('wound_record',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resident_id', sa.Integer(), nullable=False),
    sa.Column('body_zone', sa.String(length=50), nullable=False),
    sa.Column('body_x', sa.Float(), nullable=True),
    sa.Column('body_y', sa.Float(), nullable=True),
    sa.Column('wound_type', sa.String(length=30), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('size_cm', sa.String(length=20), nullable=True),
    sa.Column('severity', sa.String(length=20), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.Column('photo_path', sa.String(length=255), nullable=True),
    sa.Column('reported_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('healed_at', sa.DateTime(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['resident_id'], ['resident.id'], ),
    sa.ForeignKeyConstraint(['reported_by'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_wound_record_resident_id', 'wound_record', ['resident_id'])

    op.create_table('wound_update',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('wound_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('size_cm', sa.String(length=20), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('photo_path', sa.String(length=255), nullable=True),
    sa.Column('updated_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['wound_id'], ['wound_record.id'], ),
    sa.ForeignKeyConstraint(['updated_by'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_wound_update_wound_id', 'wound_update', ['wound_id'])


def downgrade():
    op.drop_table('wound_update')
    op.drop_table('wound_record')
