"""Add medical profile fields to resident

Revision ID: fc10d7d2195e
Revises: 29c5ab3db13a
Create Date: 2026-08-10 16:43:51.244232

"""
from alembic import op
import sqlalchemy as sa


revision = 'fc10d7d2195e'
down_revision = '29c5ab3db13a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('resident', schema=None) as batch_op:
        batch_op.add_column(sa.Column('diagnoses', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('allergies', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('current_medication', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('blood_type', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('dependency_level', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('emergency_contact_name', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('emergency_contact_phone', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('emergency_contact_relation', sa.String(length=50), nullable=True))


def downgrade():
    with op.batch_alter_table('resident', schema=None) as batch_op:
        batch_op.drop_column('emergency_contact_relation')
        batch_op.drop_column('emergency_contact_phone')
        batch_op.drop_column('emergency_contact_name')
        batch_op.drop_column('dependency_level')
        batch_op.drop_column('blood_type')
        batch_op.drop_column('current_medication')
        batch_op.drop_column('allergies')
        batch_op.drop_column('diagnoses')
