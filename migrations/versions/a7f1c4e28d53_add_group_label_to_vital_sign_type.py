"""add group_label to vital_sign_type

Revision ID: a7f1c4e28d53
Revises: d3a91b7e5c04
Create Date: 2026-09-14

Etiqueta de grupo de un campo de constantes. Los campos de un mismo tipo de
atencion que comparten etiqueta se piden y se muestran en una sola linea:
"Tension arterial: 120/80 mmHg" en vez de una sistolica y una diastolica
sueltas, que es como se estaban pidiendo.

Solo cambia como se pide y como se ensena. Cada valor sigue guardandose como su
propia lectura, con su minimo y su maximo y su propia alerta.

NULL = campo suelto, asi que nadie cambia de comportamiento por anadir la
columna y los campos que ya existen siguen igual hasta que coordinacion les
ponga grupo.

Columna nueva en tabla existente: `db.create_all()` NO la crea en el NAS, hace
falta el ALTER manual. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7f1c4e28d53'
down_revision = 'd3a91b7e5c04'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('vital_sign_type') as batch_op:
        batch_op.add_column(sa.Column('group_label', sa.String(length=60), nullable=True))


def downgrade():
    with op.batch_alter_table('vital_sign_type') as batch_op:
        batch_op.drop_column('group_label')
