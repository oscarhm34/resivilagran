"""add is_admin to cleaner

Revision ID: a3f1b2c4d5e6
Revises: 8adeefd92ca9
Create Date: 2026-04-24

"""
from alembic import op
import sqlalchemy as sa

revision = 'a3f1b2c4d5e6'
down_revision = '8adeefd92ca9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_admin', sa.Boolean(), nullable=False, server_default='0')
        )


def downgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.drop_column('is_admin')
