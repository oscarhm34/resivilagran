"""add invited confirmed fields to activity_participation

Revision ID: e882e0ca2075
Revises: 7b4bc27b2918
Create Date: 2026-08-11 16:08:03.280599

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e882e0ca2075'
down_revision = '7b4bc27b2918'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('activity_participation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('invited', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('confirmed_by', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('confirmed_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key('fk_actpart_confirmed_by', 'cleaner', ['confirmed_by'], ['id'])


def downgrade():
    with op.batch_alter_table('activity_participation', schema=None) as batch_op:
        batch_op.drop_constraint('fk_actpart_confirmed_by', type_='foreignkey')
        batch_op.drop_column('confirmed_at')
        batch_op.drop_column('confirmed_by')
        batch_op.drop_column('invited')
