"""Add indexes for performance

Revision ID: 5d65fbfa28cd
Revises: c04b94428038
Create Date: 2026-08-10 09:36:30.708859

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '5d65fbfa28cd'
down_revision = 'c04b94428038'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('absence', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_absence_end_date'), ['end_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_absence_start_date'), ['start_date'], unique=False)

    with op.batch_alter_table('care_record', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_care_record_resident_id'), ['resident_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_care_record_start_time'), ['start_time'], unique=False)
        batch_op.create_index(batch_op.f('ix_care_record_worker_id'), ['worker_id'], unique=False)

    with op.batch_alter_table('cleaning_record', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cleaning_record_cleaner_id'), ['cleaner_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_cleaning_record_room_id'), ['room_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_cleaning_record_start_time'), ['start_time'], unique=False)

    with op.batch_alter_table('shift_assignment', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shift_assignment_date'), ['date'], unique=False)


def downgrade():
    with op.batch_alter_table('shift_assignment', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shift_assignment_date'))

    with op.batch_alter_table('cleaning_record', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cleaning_record_start_time'))
        batch_op.drop_index(batch_op.f('ix_cleaning_record_room_id'))
        batch_op.drop_index(batch_op.f('ix_cleaning_record_cleaner_id'))

    with op.batch_alter_table('care_record', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_care_record_worker_id'))
        batch_op.drop_index(batch_op.f('ix_care_record_start_time'))
        batch_op.drop_index(batch_op.f('ix_care_record_resident_id'))

    with op.batch_alter_table('absence', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_absence_start_date'))
        batch_op.drop_index(batch_op.f('ix_absence_end_date'))
