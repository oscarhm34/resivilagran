"""widen app_setting value to text

Revision ID: d1a4f76b2c08
Revises: c8d3f50a6b19
Create Date: 2026-09-17

`app_setting.value` era VARCHAR(500). Servia mientras los ajustes eran un
'true' o un numero, pero ya no: `quick_phrases` guarda un JSON con todas las
frases rapidas y `chatbot_extra_prompt` guarda hasta 4000 caracteres de
instrucciones para el asistente. Pasa a TEXT, que en PostgreSQL no cuesta nada
y en SQLite es el mismo tipo de siempre.

EN EL NAS: `db.create_all()` no altera columnas que ya existen. El ALTER va a
mano, en una sola linea. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1a4f76b2c08'
down_revision = 'c8d3f50a6b19'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('value',
                              existing_type=sa.String(length=500),
                              type_=sa.Text(),
                              existing_nullable=False)


def downgrade():
    # PostgreSQL no trunca al estrechar la columna: falla si algun valor no
    # cabe. Se recortan antes, a proposito y a la vista.
    op.execute("UPDATE app_setting SET value = substr(value, 1, 500)")
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('value',
                              existing_type=sa.Text(),
                              type_=sa.String(length=500),
                              existing_nullable=False)
