"""add content translation

Revision ID: c62f4a80d371
Revises: a1d7e93c58b2
Create Date: 2026-09-07

Traducciones del contenido que escribe coordinacion: instrucciones de los tipos
de atencion, items del checklist e informacion relevante de los residentes.

Generica y no una tabla por modelo: son campos sueltos de sitios distintos y
cuatro tablas serian cuatro esquemas para el mismo problema.

`source_hash` guarda el resumen del castellano en el momento de traducir. Si el
original cambia despues, la traduccion se marca desfasada y deja de servirse.

En el NAS `db.create_all()` SI crea esta tabla al arrancar, porque es nueva.
Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c62f4a80d371'
down_revision = 'a1d7e93c58b2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'content_translation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_type', sa.String(length=30), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('field', sa.String(length=30), nullable=False),
        sa.Column('lang', sa.String(length=5), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('source_hash', sa.String(length=32), nullable=False),
        sa.Column('generated_at', sa.DateTime(), nullable=False),
        sa.Column('reviewed', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entity_type', 'entity_id', 'field', 'lang',
                            name='uq_content_translation'),
    )
    with op.batch_alter_table('content_translation', schema=None) as batch_op:
        batch_op.create_index('ix_content_translation_entity_type',
                              ['entity_type'], unique=False)
        batch_op.create_index('ix_content_translation_entity_id',
                              ['entity_id'], unique=False)
        batch_op.create_index('ix_content_translation_lang',
                              ['lang'], unique=False)
        batch_op.create_index('ix_content_translation_lookup',
                              ['entity_type', 'entity_id', 'lang'], unique=False)


def downgrade():
    op.drop_table('content_translation')
