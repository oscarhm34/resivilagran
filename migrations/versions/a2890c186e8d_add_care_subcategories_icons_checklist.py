"""add_care_subcategories_icons_checklist

Revision ID: a2890c186e8d
Revises: 9988f8672548
Create Date: 2026-07-27 08:48:30.007446

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a2890c186e8d'
down_revision = '9988f8672548'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('checklist_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('text', sa.String(length=200), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('care_record_types',
        sa.Column('care_record_id', sa.Integer(), nullable=False),
        sa.Column('care_type_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['care_record_id'], ['care_record.id']),
        sa.ForeignKeyConstraint(['care_type_id'], ['care_type.id']),
        sa.PrimaryKeyConstraint('care_record_id', 'care_type_id')
    )
    with op.batch_alter_table('care_type', schema=None) as batch_op:
        batch_op.add_column(sa.Column('icon', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('parent_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('sort_order', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('active', sa.Boolean(), nullable=True))

    with op.batch_alter_table('cleaning_record', schema=None) as batch_op:
        batch_op.add_column(sa.Column('checklist_json', sa.Text(), nullable=True))

    # Backfill icons for existing care types
    icon_map = {
        'Aseo e higiene': '🛁', 'Medicación': '💊', 'Fisioterapia': '🏃',
        'Comida': '🍽️', 'Compañía': '💬', 'Cambio de postura': '🔄',
        'Cura / Heridas': '🩹', 'Otro': '📋',
    }
    care_type = sa.table('care_type', sa.column('name', sa.String), sa.column('icon', sa.String), sa.column('active', sa.Boolean))
    for name, icon in icon_map.items():
        op.execute(care_type.update().where(care_type.c.name == name).values(icon=icon))
    # Set active=True for all existing care types
    op.execute(care_type.update().values(active=True))


def downgrade():
    with op.batch_alter_table('cleaning_record', schema=None) as batch_op:
        batch_op.drop_column('checklist_json')

    with op.batch_alter_table('care_type', schema=None) as batch_op:
        batch_op.drop_column('active')
        batch_op.drop_column('sort_order')
        batch_op.drop_column('parent_id')
        batch_op.drop_column('icon')

    op.drop_table('care_record_types')
    op.drop_table('checklist_item')
