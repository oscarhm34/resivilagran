"""Add ActivityTemplate model and template_id to Activity

Revision ID: 702bb8a226a6
Revises: e882e0ca2075
Create Date: 2026-08-12 08:33:27.532943

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '702bb8a226a6'
down_revision = 'e882e0ca2075'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('activity_template',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=150), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('weekday', sa.Integer(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=True),
    sa.Column('end_time', sa.Time(), nullable=True),
    sa.Column('location', sa.String(length=100), nullable=True),
    sa.Column('category', sa.String(length=50), nullable=True),
    sa.Column('recurrence', sa.String(length=20), nullable=True),
    sa.Column('resident_ids_json', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('activity', schema=None) as batch_op:
        batch_op.add_column(sa.Column('template_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_activity_template', 'activity_template', ['template_id'], ['id'])


def downgrade():
    with op.batch_alter_table('activity', schema=None) as batch_op:
        batch_op.drop_constraint('fk_activity_template', type_='foreignkey')
        batch_op.drop_column('template_id')

    op.drop_table('activity_template')
