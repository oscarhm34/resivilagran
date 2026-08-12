"""Add MoodRecord and ActivityPhoto models

Revision ID: 5f5463d5f9f1
Revises: 702bb8a226a6
Create Date: 2026-08-12 10:31:20.986500

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5f5463d5f9f1'
down_revision = '702bb8a226a6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('mood_record',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resident_id', sa.Integer(), nullable=False),
    sa.Column('worker_id', sa.Integer(), nullable=False),
    sa.Column('mood_score', sa.Integer(), nullable=False),
    sa.Column('behavior_flags', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['resident_id'], ['resident.id'], ),
    sa.ForeignKeyConstraint(['worker_id'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('mood_record', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_mood_record_recorded_at'), ['recorded_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_mood_record_resident_id'), ['resident_id'], unique=False)

    op.create_table('activity_photo',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('activity_id', sa.Integer(), nullable=False),
    sa.Column('photo_path', sa.String(length=255), nullable=False),
    sa.Column('caption', sa.Text(), nullable=True),
    sa.Column('uploaded_by', sa.Integer(), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['activity_id'], ['activity.id'], ),
    sa.ForeignKeyConstraint(['uploaded_by'], ['cleaner.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('activity_photo', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_activity_photo_activity_id'), ['activity_id'], unique=False)


def downgrade():
    with op.batch_alter_table('activity_photo', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_activity_photo_activity_id'))

    op.drop_table('activity_photo')
    with op.batch_alter_table('mood_record', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mood_record_resident_id'))
        batch_op.drop_index(batch_op.f('ix_mood_record_recorded_at'))

    op.drop_table('mood_record')
