"""Add incident reporting tables

Revision ID: 4aa7ba9c9210
Revises: 5d65fbfa28cd
Create Date: 2026-08-10 15:19:18.355213

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4aa7ba9c9210'
down_revision = '5d65fbfa28cd'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('incident_type',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('icon', sa.String(length=50), nullable=True),
    sa.Column('color', sa.String(length=20), nullable=True),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('incident',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_type_id', sa.Integer(), nullable=True),
    sa.Column('reported_by', sa.Integer(), nullable=False),
    sa.Column('resident_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('location', sa.String(length=100), nullable=True),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('occurred_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_by', sa.Integer(), nullable=True),
    sa.Column('resolution_notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['incident_type_id'], ['incident_type.id'], ),
    sa.ForeignKeyConstraint(['reported_by'], ['cleaner.id'], ),
    sa.ForeignKeyConstraint(['resident_id'], ['resident.id'], ),
    sa.ForeignKeyConstraint(['resolved_by'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('incident', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incident_incident_type_id'), ['incident_type_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_incident_occurred_at'), ['occurred_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_incident_reported_by'), ['reported_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_incident_resident_id'), ['resident_id'], unique=False)


def downgrade():
    with op.batch_alter_table('incident', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incident_resident_id'))
        batch_op.drop_index(batch_op.f('ix_incident_reported_by'))
        batch_op.drop_index(batch_op.f('ix_incident_occurred_at'))
        batch_op.drop_index(batch_op.f('ix_incident_incident_type_id'))

    op.drop_table('incident')
    op.drop_table('incident_type')
