"""Add barcode field to medication_prescription

Revision ID: a1b2c3d4e5f6
Revises: e557b0a636af
Create Date: 2026-08-13

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'e557b0a636af'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('medication_prescription', schema=None) as batch_op:
        batch_op.add_column(sa.Column('barcode', sa.String(length=100), nullable=True))


def downgrade():
    with op.batch_alter_table('medication_prescription', schema=None) as batch_op:
        batch_op.drop_column('barcode')
