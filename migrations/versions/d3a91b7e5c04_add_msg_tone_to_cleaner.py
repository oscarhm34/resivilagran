"""add msg_tone to cleaner

Revision ID: d3a91b7e5c04
Revises: c62f4a80d371
Create Date: 2026-09-09

Tono del aviso de mensaje elegido por cada trabajadora. Se guarda en el perfil y
no solo en el movil para que la siga si cambia de telefono, igual que `lang`.

NULL = el tono de siempre, asi que nadie cambia de sonido por anadir la columna.

El servidor lo necesita ademas para meter el patron de vibracion en el aviso
push: con la aplicacion cerrada el sonido lo decide Android y la web no puede
tocarlo, asi que la vibracion es lo unico que distingue un tono de otro.

Columna nueva en tabla existente: `db.create_all()` NO la crea en el NAS, hace
falta el ALTER manual. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd3a91b7e5c04'
down_revision = 'c62f4a80d371'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cleaner') as batch_op:
        batch_op.add_column(sa.Column('msg_tone', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('cleaner') as batch_op:
        batch_op.drop_column('msg_tone')
