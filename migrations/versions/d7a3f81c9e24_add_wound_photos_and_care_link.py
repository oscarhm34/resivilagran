"""add wound photos and care link

Revision ID: d7a3f81c9e24
Revises: c4f9a1e7b028
Create Date: 2026-10-27

Las fotos de una cura y el enlace con la atencion en la que se hizo.

`wound_update.photo_path` existia desde agosto y no se escribio nunca: una sola
foto no sirve, porque una cura se retrata desde varios angulos. Las fotos pasan a
ser filas de `wound_photo`, colgando del parte de seguimiento y no de la herida,
porque una foto de una cura solo significa algo con su fecha y su estado al lado.

`care_record_id` es nullable a proposito: desde el panel se anade un parte sin
pasar por ninguna atencion.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd7a3f81c9e24'
down_revision = 'c4f9a1e7b028'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'wound_photo',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('wound_update_id', sa.Integer(), nullable=False),
        sa.Column('photo_path', sa.String(length=255), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['wound_update_id'], ['wound_update.id'],
                                name='fk_wound_photo_update'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('wound_photo', schema=None) as batch_op:
        batch_op.create_index('ix_wound_photo_wound_update_id',
                              ['wound_update_id'], unique=False)

    with op.batch_alter_table('wound_update', schema=None) as batch_op:
        batch_op.add_column(sa.Column('care_record_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_wound_update_care_record_id',
                              ['care_record_id'], unique=False)
        batch_op.create_foreign_key('fk_wound_update_care_record', 'care_record',
                                    ['care_record_id'], ['id'], ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('wound_update', schema=None) as batch_op:
        batch_op.drop_constraint('fk_wound_update_care_record', type_='foreignkey')
        batch_op.drop_index('ix_wound_update_care_record_id')
        batch_op.drop_column('care_record_id')

    with op.batch_alter_table('wound_photo', schema=None) as batch_op:
        batch_op.drop_index('ix_wound_photo_wound_update_id')
    op.drop_table('wound_photo')
