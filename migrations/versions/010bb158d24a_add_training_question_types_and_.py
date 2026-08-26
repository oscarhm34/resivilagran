"""add training question types and instruction translations

Revision ID: 010bb158d24a
Revises: e22a76ba3c1b
Create Date: 2026-08-26

Escrita a mano: el autogenerate arrastraba deriva no relacionada de la BD de
desarrollo, asi que solo se conservan los cambios de esta funcionalidad.
"""
from alembic import op
import sqlalchemy as sa

revision = '010bb158d24a'
down_revision = 'e22a76ba3c1b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'training_translation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('question_id', sa.Integer(), nullable=False),
        sa.Column('lang', sa.String(length=5), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('yes_label', sa.String(length=50), nullable=False),
        sa.Column('no_label', sa.String(length=50), nullable=False),
        sa.Column('audio_path', sa.String(length=255), nullable=True),
        sa.Column('generated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['question_id'], ['training_question.id'],
                                name='fk_tt_question'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('question_id', 'lang', name='uq_training_translation'),
    )
    with op.batch_alter_table('training_translation', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_training_translation_question_id'),
                              ['question_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_training_translation_lang'),
                              ['lang'], unique=False)

    with op.batch_alter_table('training_question', schema=None) as batch_op:
        batch_op.add_column(sa.Column('question_type', sa.String(length=20),
                                      nullable=False, server_default='multiple'))
        batch_op.alter_column('question_text',
                              existing_type=sa.VARCHAR(length=500),
                              type_=sa.Text(),
                              existing_nullable=False)


def downgrade():
    with op.batch_alter_table('training_question', schema=None) as batch_op:
        batch_op.alter_column('question_text',
                              existing_type=sa.Text(),
                              type_=sa.VARCHAR(length=500),
                              existing_nullable=False)
        batch_op.drop_column('question_type')

    with op.batch_alter_table('training_translation', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_training_translation_lang'))
        batch_op.drop_index(batch_op.f('ix_training_translation_question_id'))
    op.drop_table('training_translation')
