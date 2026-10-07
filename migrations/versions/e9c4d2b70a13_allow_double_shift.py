"""allow double shift

Revision ID: e9c4d2b70a13
Revises: d7a2b4e81f05
Create Date: 2026-10-07

Quita el unico que impedia que una trabajadora tuviera dos puestos el mismo dia.

Doblar un M1 con un T1 pasa cuando falta alguien, y hasta ahora la base de datos
lo prohibia: la unica salida era no apuntarlo, que es justo lo contrario de para
lo que sirve el cuadrante. El limite de que un puesto lo ocupe una sola persona
(uq_date_position) se queda.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e9c4d2b70a13'
down_revision = 'd7a2b4e81f05'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('shift_assignment', schema=None) as batch_op:
        batch_op.drop_constraint('uq_worker_date', type_='unique')


def downgrade():
    # Solo se puede volver atras si nadie dobla: con dos puestos el mismo dia,
    # recrear el unico falla. Hay que decidir antes con cual quedarse.
    with op.batch_alter_table('shift_assignment', schema=None) as batch_op:
        batch_op.create_unique_constraint('uq_worker_date', ['cleaner_id', 'date'])
