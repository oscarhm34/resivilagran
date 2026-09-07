"""add room type to checklist item

Revision ID: d4a91f26c8b7
Revises: b3e7c9d15a24
Create Date: 2026-09-07

Cada item del checklist pasa a pertenecer a un tipo de zona. La columna es
nullable porque los items que ya existen no tienen ninguna: un NOT NULL haria
fallar el ALTER sobre la tabla con datos. Un item sin zona no sale en ninguna
limpieza, y el panel lo marca para que se le asigne una.

En el NAS `db.create_all()` no anade columnas a tablas que ya existen, hace
falta el ALTER a mano. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4a91f26c8b7'
down_revision = 'b3e7c9d15a24'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('checklist_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('room_type_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_checklist_item_room_type_id'),
                              ['room_type_id'], unique=False)
        batch_op.create_foreign_key('fk_chk_room_type', 'room_type',
                                    ['room_type_id'], ['id'])


def downgrade():
    with op.batch_alter_table('checklist_item', schema=None) as batch_op:
        batch_op.drop_constraint('fk_chk_room_type', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_checklist_item_room_type_id'))
        batch_op.drop_column('room_type_id')
