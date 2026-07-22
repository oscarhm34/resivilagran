"""add_photo_and_relevant_info_to_resident

Revision ID: 9988f8672548
Revises: fb8d2aa0a90f
Create Date: 2026-07-23 00:08:33.073534

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9988f8672548'
down_revision = 'fb8d2aa0a90f'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('resident', schema=None) as batch_op:
        batch_op.add_column(sa.Column('photo_path', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('relevant_info', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('resident', schema=None) as batch_op:
        batch_op.drop_column('relevant_info')
        batch_op.drop_column('photo_path')
