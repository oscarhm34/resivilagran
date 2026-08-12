"""Add AuditLog model

Revision ID: cbc979817329
Revises: b45331d96767
Create Date: 2026-08-12 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'cbc979817329'
down_revision = 'b45331d96767'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=20), nullable=False),
    sa.Column('table_name', sa.String(length=50), nullable=False),
    sa.Column('record_id', sa.Integer(), nullable=True),
    sa.Column('details', sa.Text(), nullable=True),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_log_created_at'), ['created_at'], unique=False)


def downgrade():
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_created_at'))
    op.drop_table('audit_log')
