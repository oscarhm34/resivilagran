"""Add icon_path to CareType

Revision ID: c8c3efeb026d
Revises: a2890c186e8d
Create Date: 2026-07-30 08:15:45.355987

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8c3efeb026d'
down_revision = 'a2890c186e8d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('care_type', schema=None) as batch_op:
        batch_op.add_column(sa.Column('icon_path', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('care_type', schema=None) as batch_op:
        batch_op.drop_column('icon_path')
