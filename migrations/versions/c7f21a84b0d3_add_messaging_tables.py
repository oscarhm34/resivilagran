"""add messaging tables

Revision ID: c7f21a84b0d3
Revises: 010bb158d24a
Create Date: 2026-08-31

Escrita a mano, como la anterior: la BD de desarrollo esta por detras del head y
el autogenerate arrastraba deriva no relacionada. Aqui solo estan las cuatro
tablas de la mensajeria; no se toca ninguna tabla existente.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7f21a84b0d3'
down_revision = '010bb158d24a'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'conversation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('title', sa.String(length=100), nullable=True),
        sa.Column('dm_key', sa.String(length=32), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('last_message_at', sa.DateTime(), nullable=True),
        sa.Column('last_message_preview', sa.String(length=140), nullable=True),
        sa.Column('last_message_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['cleaner.id'],
                                name='fk_conversation_created_by'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dm_key', name='uq_conversation_dm_key'),
    )
    with op.batch_alter_table('conversation', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversation_created_by'), ['created_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_last_message_at'), ['last_message_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_last_message_id'), ['last_message_id'], unique=False)

    op.create_table(
        'conversation_member',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('cleaner_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=10), nullable=False),
        sa.Column('joined_at', sa.DateTime(), nullable=False),
        sa.Column('left_at', sa.DateTime(), nullable=True),
        sa.Column('left_at_message_id', sa.Integer(), nullable=False),
        sa.Column('last_read_message_id', sa.Integer(), nullable=False),
        sa.Column('last_read_at', sa.DateTime(), nullable=True),
        sa.Column('cleared_before_id', sa.Integer(), nullable=False),
        sa.Column('muted_until', sa.DateTime(), nullable=True),
        sa.Column('archived', sa.Boolean(), nullable=False),
        sa.Column('last_push_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'],
                                name='fk_convmember_conversation'),
        sa.ForeignKeyConstraint(['cleaner_id'], ['cleaner.id'],
                                name='fk_convmember_cleaner'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('conversation_id', 'cleaner_id', name='uq_conversation_member'),
    )
    with op.batch_alter_table('conversation_member', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversation_member_conversation_id'),
                              ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_member_cleaner_id'),
                              ['cleaner_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_member_left_at'), ['left_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_member_last_read_message_id'),
                              ['last_read_message_id'], unique=False)
        batch_op.create_index('ix_convmember_cleaner_archived',
                              ['cleaner_id', 'archived'], unique=False)

    op.create_table(
        'message',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('sender_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('client_uuid', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('edited_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'],
                                name='fk_message_conversation'),
        sa.ForeignKeyConstraint(['sender_id'], ['cleaner.id'], name='fk_message_sender'),
        sa.ForeignKeyConstraint(['deleted_by_id'], ['cleaner.id'], name='fk_message_deleted_by'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sender_id', 'client_uuid', name='uq_message_client_uuid'),
    )
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_message_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_sender_id'), ['sender_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_deleted_at'), ['deleted_at'], unique=False)
        batch_op.create_index('ix_message_conversation_id_id',
                              ['conversation_id', 'id'], unique=False)
        batch_op.create_index('ix_message_conversation_created',
                              ['conversation_id', 'created_at'], unique=False)

    op.create_table(
        'message_attachment',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=False),
        sa.Column('media_type', sa.String(length=10), nullable=False),
        sa.Column('file_path', sa.String(length=255), nullable=False),
        sa.Column('thumb_path', sa.String(length=255), nullable=True),
        sa.Column('mime_type', sa.String(length=50), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('original_filename', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['message_id'], ['message.id'], name='fk_attachment_message'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('message_attachment', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_message_attachment_message_id'),
                              ['message_id'], unique=False)


def downgrade():
    op.drop_table('message_attachment')
    op.drop_table('message')
    op.drop_table('conversation_member')
    op.drop_table('conversation')
