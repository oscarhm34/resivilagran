"""add lang to cleaner

Revision ID: a1d7e93c58b2
Revises: f8c3b21e4a09
Create Date: 2026-09-07

Idioma de la webapp por trabajadora. Nullable, y NULL significa castellano: asi
nadie cambia de idioma por el hecho de anadir la columna.

En el NAS `db.create_all()` no anade columnas a tablas que ya existen, hace
falta el ALTER a mano. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1d7e93c58b2'
down_revision = 'f8c3b21e4a09'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lang', sa.String(length=5), nullable=True))


def downgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.drop_column('lang')
