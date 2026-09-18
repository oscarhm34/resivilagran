"""add belonging image descriptors

Revision ID: f3c81e560b47
Revises: e5b9c1d740a2
Create Date: 2026-09-18

Tres columnas en `resident_belonging` para poder identificar un objeto perdido
comparando su foto con el inventario: el vector de la imagen (DINOv2-small),
el histograma de color y la version del descriptor con que se calcularon.

Los dos vectores van como LargeBinary (BYTEA en PostgreSQL, BLOB en SQLite) en
vez de pgvector: con unos miles de fotos la comparacion es un producto de
matrices de numpy, y una extension de PostgreSQL nueva en el NAS no compensa.

`descriptor_version` esta indexada porque toda consulta filtra por ella: si se
cambia como se describe la imagen, los descriptores viejos dejan de ser
comparables y hay que recalcularlos con `flask backfill-descriptores`.

EN EL NAS: estas son columnas NUEVAS en una tabla que YA EXISTE, y
`db.create_all()` no anade columnas. Hay que hacer el ALTER a mano, en una
linea, contra PostgreSQL. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3c81e560b47'
down_revision = 'e5b9c1d740a2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('resident_belonging') as batch_op:
        batch_op.add_column(sa.Column('embedding', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('color_hist', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('descriptor_version', sa.String(length=40), nullable=True))
        batch_op.create_index('ix_resident_belonging_descriptor_version',
                              ['descriptor_version'])


def downgrade():
    with op.batch_alter_table('resident_belonging') as batch_op:
        batch_op.drop_index('ix_resident_belonging_descriptor_version')
        batch_op.drop_column('descriptor_version')
        batch_op.drop_column('color_hist')
        batch_op.drop_column('embedding')
