"""Add last_active to Cleaner

Revision ID: e22a76ba3c1b
Revises: b2c3d4e5f6a7
Create Date: 2026-08-20 14:47:37.381595

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e22a76ba3c1b'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_active', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_cleaner_last_active'), ['last_active'], unique=False)


def downgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cleaner_last_active'))
        batch_op.drop_column('last_active')
