"""add_active_to_cleaner

Revision ID: e5f6a7b8c9d0
Revises: ddfe3820d2a9
Create Date: 2026-07-15 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
down_revision = 'ddfe3820d2a9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.add_column(sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('1')))


def downgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.drop_column('active')
