"""Add AppSetting model

Revision ID: 9fb1fa2626a1
Revises: f42ec11d0e1d
Create Date: 2026-08-03

"""
from alembic import op
import sqlalchemy as sa

revision = '9fb1fa2626a1'
down_revision = 'f42ec11d0e1d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('app_setting',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.String(length=500), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.execute("INSERT INTO app_setting (key, value) VALUES ('allow_group_care', 'true')")
    op.execute("INSERT INTO app_setting (key, value) VALUES ('nfc_only', 'true')")


def downgrade():
    op.drop_table('app_setting')
