"""Add ResidentDocument model

Revision ID: 0e3f9e717545
Revises: c8c3efeb026d
Create Date: 2026-07-30 08:54:25.775028

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0e3f9e717545'
down_revision = 'c8c3efeb026d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('resident_document',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resident_id', sa.Integer(), nullable=False),
    sa.Column('file_path', sa.String(length=255), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('doc_type', sa.String(length=50), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['resident_id'], ['resident.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('resident_document')
