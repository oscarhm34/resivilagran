"""add schedule and instructions to CareType

Revision ID: b3e7c9d15a24
Revises: c7f21a84b0d3
Create Date: 2026-09-01

Tres columnas nuevas en una tabla que ya existe: en el NAS `db.create_all()` no
las anade, hace falta el ALTER a mano. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b3e7c9d15a24'
down_revision = 'c7f21a84b0d3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('care_type', schema=None) as batch_op:
        batch_op.add_column(sa.Column('start_time', sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('end_time', sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('instructions', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('care_type', schema=None) as batch_op:
        batch_op.drop_column('instructions')
        batch_op.drop_column('end_time')
        batch_op.drop_column('start_time')
