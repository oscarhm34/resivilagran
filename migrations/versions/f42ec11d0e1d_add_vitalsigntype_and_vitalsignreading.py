"""Add VitalSignType and VitalSignReading

Revision ID: f42ec11d0e1d
Revises: 0e3f9e717545
Create Date: 2026-08-03 08:27:17.077368

"""
from alembic import op
import sqlalchemy as sa


revision = 'f42ec11d0e1d'
down_revision = '0e3f9e717545'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('vital_sign_type',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('care_type_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('unit', sa.String(length=30), nullable=False),
    sa.Column('min_value', sa.Float(), nullable=True),
    sa.Column('max_value', sa.Float(), nullable=True),
    sa.Column('input_type', sa.String(length=20), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.ForeignKeyConstraint(['care_type_id'], ['care_type.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('vital_sign_reading',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('care_record_id', sa.Integer(), nullable=False),
    sa.Column('vital_sign_type_id', sa.Integer(), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('recorded_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['care_record_id'], ['care_record.id'], ),
    sa.ForeignKeyConstraint(['vital_sign_type_id'], ['vital_sign_type.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('vital_sign_reading')
    op.drop_table('vital_sign_type')
