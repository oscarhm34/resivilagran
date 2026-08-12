"""Add TrainingAssignment and assign_mode/mandatory to TrainingPill

Revision ID: bdd3f1e2d1c5
Revises: 5f5463d5f9f1
Create Date: 2026-08-12 11:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bdd3f1e2d1c5'
down_revision = '5f5463d5f9f1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('training_assignment',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pill_id', sa.Integer(), nullable=False),
    sa.Column('cleaner_id', sa.Integer(), nullable=False),
    sa.Column('assigned_at', sa.DateTime(), nullable=True),
    sa.Column('assigned_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['assigned_by'], ['cleaner.id'], ),
    sa.ForeignKeyConstraint(['cleaner_id'], ['cleaner.id'], ),
    sa.ForeignKeyConstraint(['pill_id'], ['training_pill.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('pill_id', 'cleaner_id', name='uq_training_assignment')
    )

    with op.batch_alter_table('training_pill', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assign_mode', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('mandatory', sa.Boolean(), nullable=True))


def downgrade():
    with op.batch_alter_table('training_pill', schema=None) as batch_op:
        batch_op.drop_column('mandatory')
        batch_op.drop_column('assign_mode')

    op.drop_table('training_assignment')
