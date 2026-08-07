"""add role to cleaner

Revision ID: f5dc25be443b
Revises: 33c1334dafa8
Create Date: 2026-08-07 14:33:32.012942

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f5dc25be443b'
down_revision = '33c1334dafa8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(length=20), nullable=False, server_default='atenciones'))


def downgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.drop_column('role')
