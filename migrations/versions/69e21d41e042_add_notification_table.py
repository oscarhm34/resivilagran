"""Add notification table

Revision ID: 69e21d41e042
Revises: 4aa7ba9c9210
Create Date: 2026-08-10 15:34:05.351059

"""
from alembic import op
import sqlalchemy as sa


revision = '69e21d41e042'
down_revision = '4aa7ba9c9210'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('notification',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=30), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('severity', sa.String(length=20), nullable=True),
    sa.Column('link', sa.String(length=500), nullable=True),
    sa.Column('read', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('resident_id', sa.Integer(), nullable=True),
    sa.Column('worker_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['resident_id'], ['resident.id'], ),
    sa.ForeignKeyConstraint(['worker_id'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_notification_read'), ['read'], unique=False)


def downgrade():
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_read'))
        batch_op.drop_index(batch_op.f('ix_notification_created_at'))

    op.drop_table('notification')
