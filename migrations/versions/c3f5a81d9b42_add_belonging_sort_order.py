"""add belonging sort order

Revision ID: c3f5a81d9b42
Revises: b7e4c02f915d
Create Date: 2026-09-29

Orden manual del inventario: `sort_order` en las pertenencias (para ordenar los
objetos de un residente) y en sus fotos (para decidir cual es la portada).

Las pertenencias se quedan a 0, que significa "sin ordenar a mano": el listado
las sigue sacando por fecha descendente, igual que antes. Las fotos, en cambio,
se rellenan con 1..N por objeto: si quedaran a 0, la primera foto anadida
despues de esta migracion entraria con max+1 = 1 y se colocaria delante de las
que ya estaban.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3f5a81d9b42'
down_revision = 'b7e4c02f915d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('resident_belonging', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sort_order', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.create_index(batch_op.f('ix_resident_belonging_sort_order'),
                              ['sort_order'], unique=False)

    with op.batch_alter_table('belonging_photo', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sort_order', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.create_index(batch_op.f('ix_belonging_photo_sort_order'),
                              ['sort_order'], unique=False)

    # Backfill de las fotos: 1..N por objeto, en el orden en que se registraron.
    # Se hace fila a fila desde Python y no con una ventana SQL para que valga
    # igual en SQLite (desarrollo y tests) que en PostgreSQL (produccion).
    bind = op.get_bind()
    filas = bind.execute(sa.text(
        'SELECT id, belonging_id FROM belonging_photo ORDER BY belonging_id, id'
    )).fetchall()
    posicion = {}
    for foto_id, belonging_id in filas:
        posicion[belonging_id] = posicion.get(belonging_id, 0) + 1
        bind.execute(sa.text('UPDATE belonging_photo SET sort_order = :n WHERE id = :id'),
                     {'n': posicion[belonging_id], 'id': foto_id})


def downgrade():
    with op.batch_alter_table('belonging_photo', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_belonging_photo_sort_order'))
        batch_op.drop_column('sort_order')

    with op.batch_alter_table('resident_belonging', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_resident_belonging_sort_order'))
        batch_op.drop_column('sort_order')
