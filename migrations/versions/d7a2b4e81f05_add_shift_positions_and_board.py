"""add shift positions and board

Revision ID: d7a2b4e81f05
Revises: c3f5a81d9b42
Create Date: 2026-10-07

El puesto dentro del turno (M1, M2, CRM, RFM, NIT A...), que hasta ahora no
existia: el unico eje era el tipo de turno, o sea el tramo horario, y con eso se
sabe que alguien trabaja de manana pero no si es la coordinadora.

Trae ademas lo que hace falta para el tablero: donde se dibuja cada turno, el
reflejo de las casillas de noche dentro de la manana y de la tarde, el color y
el telefono de cada persona, y la semana publicada.

Todo lo nuevo es opcional. Las asignaciones que ya existen se quedan sin puesto
y se comportan igual que antes, y los turnos sin `board_row` no salen en el
tablero hasta que alguien los coloque.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd7a2b4e81f05'
down_revision = 'c3f5a81d9b42'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'shift_position',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('shift_type_id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=8), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('echo_row', sa.String(length=10), nullable=True),
        sa.Column('echo_previous_day', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(['shift_type_id'], ['shift_type.id'],
                                name='fk_position_shift_type'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('shift_type_id', 'code', name='uq_position_code'),
    )
    with op.batch_alter_table('shift_position', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shift_position_shift_type_id'),
                              ['shift_type_id'], unique=False)

    op.create_table(
        'shift_week_publication',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('week_start', sa.Date(), nullable=False),
        sa.Column('published_at', sa.DateTime(), nullable=False),
        sa.Column('published_by', sa.Integer(), nullable=True),
        sa.Column('notified_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['published_by'], ['cleaner.id'],
                                name='fk_week_pub_cleaner'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('week_start'),
    )
    with op.batch_alter_table('shift_week_publication', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shift_week_publication_week_start'),
                              ['week_start'], unique=False)

    with op.batch_alter_table('shift_type', schema=None) as batch_op:
        batch_op.add_column(sa.Column('board_row', sa.String(length=10), nullable=True))

    with op.batch_alter_table('shift_assignment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('position_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_shift_assignment_position_id'),
                              ['position_id'], unique=False)
        batch_op.create_foreign_key('fk_assignment_position', 'shift_position',
                                    ['position_id'], ['id'])
        # Un puesto, una persona al dia. Los NULL no chocan entre si ni en
        # PostgreSQL ni en SQLite, asi que lo que no tiene puesto convive.
        batch_op.create_unique_constraint('uq_date_position', ['date', 'position_id'])

    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.add_column(sa.Column('color', sa.String(length=7), nullable=True))
        batch_op.add_column(sa.Column('phone', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('cleaner', schema=None) as batch_op:
        batch_op.drop_column('phone')
        batch_op.drop_column('color')

    with op.batch_alter_table('shift_assignment', schema=None) as batch_op:
        batch_op.drop_constraint('uq_date_position', type_='unique')
        batch_op.drop_constraint('fk_assignment_position', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_shift_assignment_position_id'))
        batch_op.drop_column('updated_at')
        batch_op.drop_column('position_id')

    with op.batch_alter_table('shift_type', schema=None) as batch_op:
        batch_op.drop_column('board_row')

    with op.batch_alter_table('shift_week_publication', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shift_week_publication_week_start'))
    op.drop_table('shift_week_publication')

    with op.batch_alter_table('shift_position', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shift_position_shift_type_id'))
    op.drop_table('shift_position')
