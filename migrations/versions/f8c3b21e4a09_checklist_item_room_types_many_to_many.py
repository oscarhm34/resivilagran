"""checklist item to room types, many to many

Revision ID: f8c3b21e4a09
Revises: d4a91f26c8b7
Create Date: 2026-09-07

La FK simple de la revision anterior no aguanta los datos reales: de los siete
items que hay en produccion, ninguno es de una sola zona. "Luces apagadas" toca
en las nueve y "papel WC" en banos y apartamentos, asi que con una zona por item
habria que duplicarlos y repasar la lista entera cada vez que se da de alta un
tipo de zona nuevo.

No hay datos que migrar: `room_type_id` se anadio hoy y esta a NULL en las siete
filas, asi que la columna se retira sin perder nada.

En el NAS `db.create_all()` SI crea la tabla nueva al arrancar, pero NO retira la
columna vieja: ese DROP va a mano. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f8c3b21e4a09'
down_revision = 'd4a91f26c8b7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'checklist_item_room_types',
        sa.Column('checklist_item_id', sa.Integer(), nullable=False),
        sa.Column('room_type_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['checklist_item_id'], ['checklist_item.id'],
                                name='fk_chkrt_item'),
        sa.ForeignKeyConstraint(['room_type_id'], ['room_type.id'],
                                name='fk_chkrt_room_type'),
        sa.PrimaryKeyConstraint('checklist_item_id', 'room_type_id'),
    )
    with op.batch_alter_table('checklist_item', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_checklist_item_room_type_id'))
        batch_op.drop_column('room_type_id')


def downgrade():
    with op.batch_alter_table('checklist_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('room_type_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_checklist_item_room_type_id'),
                              ['room_type_id'], unique=False)
        batch_op.create_foreign_key('fk_chk_room_type', 'room_type',
                                    ['room_type_id'], ['id'])
    op.drop_table('checklist_item_room_types')
