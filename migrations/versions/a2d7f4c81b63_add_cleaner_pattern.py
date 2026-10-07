"""add cleaner pattern

Revision ID: a2d7f4c81b63
Revises: f1b6c3e90d27
Create Date: 2026-10-07

Como se pinta cada persona en el tablero, ademas de con que color. Quince
colores no llegan para una plantilla de treinta, y dos naranjas iguales en la
vista del mes no se distinguen de un vistazo, que es justo para lo que sirve esa
vista.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a2d7f4c81b63'
down_revision = 'f1b6c3e90d27'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pattern', sa.String(length=10), nullable=True))


def downgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.drop_column('pattern')
