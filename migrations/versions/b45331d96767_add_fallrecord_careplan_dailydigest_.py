"""Add FallRecord, CarePlan, DailyDigest, MealRecord and is_fall to Incident

Revision ID: b45331d96767
Revises: bdd3f1e2d1c5
Create Date: 2026-08-12 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'b45331d96767'
down_revision = 'bdd3f1e2d1c5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('care_plan',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resident_id', sa.Integer(), nullable=False),
    sa.Column('objectives', sa.Text(), nullable=True),
    sa.Column('interventions', sa.Text(), nullable=True),
    sa.Column('html_content', sa.Text(), nullable=True),
    sa.Column('review_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.Column('ai_review', sa.Text(), nullable=True),
    sa.Column('ai_review_date', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['cleaner.id'], ),
    sa.ForeignKeyConstraint(['resident_id'], ['resident.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('care_plan', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_care_plan_resident_id'), ['resident_id'], unique=False)

    op.create_table('daily_digest',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resident_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('html_content', sa.Text(), nullable=False),
    sa.Column('highlights', sa.Text(), nullable=True),
    sa.Column('has_alerts', sa.Boolean(), nullable=True),
    sa.Column('generated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['resident_id'], ['resident.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('resident_id', 'date', name='uq_daily_digest')
    )
    with op.batch_alter_table('daily_digest', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_daily_digest_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_daily_digest_resident_id'), ['resident_id'], unique=False)

    op.create_table('meal_record',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resident_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('meal_type', sa.String(length=20), nullable=False),
    sa.Column('intake_pct', sa.Integer(), nullable=False),
    sa.Column('fluid_ml', sa.Integer(), nullable=True),
    sa.Column('texture', sa.String(length=30), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('recorded_by', sa.Integer(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['recorded_by'], ['cleaner.id'], ),
    sa.ForeignKeyConstraint(['resident_id'], ['resident.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('resident_id', 'date', 'meal_type', name='uq_meal_record')
    )
    with op.batch_alter_table('meal_record', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_meal_record_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_meal_record_resident_id'), ['resident_id'], unique=False)

    op.create_table('fall_record',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.Integer(), nullable=False),
    sa.Column('fall_location', sa.String(length=100), nullable=True),
    sa.Column('activity_at_time', sa.String(length=100), nullable=True),
    sa.Column('footwear', sa.String(length=50), nullable=True),
    sa.Column('witnesses', sa.Text(), nullable=True),
    sa.Column('injuries', sa.Text(), nullable=True),
    sa.Column('injury_severity', sa.String(length=20), nullable=True),
    sa.Column('measures_taken', sa.Text(), nullable=True),
    sa.Column('contributing_factors', sa.Text(), nullable=True),
    sa.Column('post_fall_vitals', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('incident_id')
    )

    with op.batch_alter_table('incident', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_fall', sa.Boolean(), nullable=True))


def downgrade():
    with op.batch_alter_table('incident', schema=None) as batch_op:
        batch_op.drop_column('is_fall')

    op.drop_table('fall_record')
    with op.batch_alter_table('meal_record', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_meal_record_resident_id'))
        batch_op.drop_index(batch_op.f('ix_meal_record_date'))
    op.drop_table('meal_record')
    with op.batch_alter_table('daily_digest', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_daily_digest_resident_id'))
        batch_op.drop_index(batch_op.f('ix_daily_digest_date'))
    op.drop_table('daily_digest')
    with op.batch_alter_table('care_plan', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_care_plan_resident_id'))
    op.drop_table('care_plan')
