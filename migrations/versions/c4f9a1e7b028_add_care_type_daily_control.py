"""add care type daily control

Revision ID: c4f9a1e7b028
Revises: a2d7f4c81b63
Create Date: 2026-10-27

Que atenciones se repasan una por una cada dia: levantar, acostar, WC. El
informe de control diario marca al residente que no la ha recibido. Hasta ahora
eso se hacia a mano sobre un Excel, de madrugada y a ojo.

`minimo_diario` distingue "no ha ido al WC" de "solo ha ido una vez de dos", y
`control_etiqueta` existe porque "NO WC" cabe en la hoja impresa y
"NO ACOMPANAR AL WC" no. Las dos a NULL se comportan como el valor por defecto.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4f9a1e7b028'
down_revision = 'a2d7f4c81b63'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('care_type', schema=None) as batch_op:
        # server_default ademas del default de Python: sin el, el ALTER sobre
        # una tabla con filas deja NULL en una columna NOT NULL y falla.
        batch_op.add_column(sa.Column('control_diario', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('minimo_diario', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('control_etiqueta', sa.String(length=12), nullable=True))


def downgrade():
    with op.batch_alter_table('care_type', schema=None) as batch_op:
        batch_op.drop_column('control_etiqueta')
        batch_op.drop_column('minimo_diario')
        batch_op.drop_column('control_diario')
