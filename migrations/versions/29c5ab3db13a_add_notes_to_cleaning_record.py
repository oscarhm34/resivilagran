"""Add notes to cleaning_record

Revision ID: 29c5ab3db13a
Revises: 69e21d41e042
Create Date: 2026-08-10 16:27:22.951770

"""
from alembic import op
import sqlalchemy as sa


revision = '29c5ab3db13a'
down_revision = '69e21d41e042'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cleaning_record', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('cleaning_record', schema=None) as batch_op:
        batch_op.drop_column('notes')
