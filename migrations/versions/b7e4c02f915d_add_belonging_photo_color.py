"""add belonging photo color

Revision ID: b7e4c02f915d
Revises: a92d47f1c3e8
Create Date: 2026-09-24

El color dominante de cada foto, para poder filtrar el inventario por color.
Se rellena con `flask backfill-colores`: las fotos ya registradas no lo traen y
sin el no saldrian en el filtro.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7e4c02f915d'
down_revision = 'a92d47f1c3e8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('belonging_photo', schema=None) as batch_op:
        batch_op.add_column(sa.Column('color', sa.String(length=12), nullable=True))
        batch_op.create_index(batch_op.f('ix_belonging_photo_color'), ['color'], unique=False)


def downgrade():
    with op.batch_alter_table('belonging_photo', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_belonging_photo_color'))
        batch_op.drop_column('color')
