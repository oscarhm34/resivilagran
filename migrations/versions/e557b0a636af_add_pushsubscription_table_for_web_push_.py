"""Add PushSubscription table for web push notifications

Revision ID: e557b0a636af
Revises: cbc979817329
Create Date: 2026-08-13 08:33:58.036303

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e557b0a636af'
down_revision = 'cbc979817329'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('push_subscription',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('worker_id', sa.Integer(), nullable=False),
    sa.Column('endpoint', sa.Text(), nullable=False),
    sa.Column('keys_json', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['worker_id'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('endpoint')
    )


def downgrade():
    op.drop_table('push_subscription')
