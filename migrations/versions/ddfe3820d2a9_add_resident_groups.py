"""add_resident_groups

Revision ID: ddfe3820d2a9
Revises: c1d2e3f4a5b6
Create Date: 2026-07-10 10:37:01.322381

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ddfe3820d2a9'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Crear tabla resident_group si no existe
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()

    if 'resident_group' not in tables:
        op.create_table(
            'resident_group',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(50), unique=True, nullable=False),
            sa.Column('color', sa.String(7), nullable=False),
        )

    # 2. Crear tabla cleaner_groups si no existe
    if 'cleaner_groups' not in tables:
        op.create_table(
            'cleaner_groups',
            sa.Column('cleaner_id', sa.Integer(), sa.ForeignKey('cleaner.id'), primary_key=True),
            sa.Column('group_id', sa.Integer(), sa.ForeignKey('resident_group.id'), primary_key=True),
        )

    # 3. Añadir group_id a resident si no existe
    columns = [c['name'] for c in sa.inspect(conn).get_columns('resident')]
    if 'group_id' not in columns:
        with op.batch_alter_table('resident', schema=None) as batch_op:
            batch_op.add_column(sa.Column('group_id', sa.Integer(), nullable=True))
            batch_op.create_foreign_key('fk_resident_group_id', 'resident_group', ['group_id'], ['id'])

    # 4. Migrar rooms con tipo RESIDENT a la tabla resident
    room_type = conn.execute(
        sa.text("SELECT id FROM room_type WHERE name = 'RESIDENT'")
    ).fetchone()

    if room_type:
        rooms = conn.execute(
            sa.text("SELECT number, description FROM room WHERE room_type_id = :tid"),
            {'tid': room_type[0]},
        ).fetchall()

        for number, description in rooms:
            existing = conn.execute(
                sa.text("SELECT id FROM resident WHERE nfc_code = :code"),
                {'code': number},
            ).fetchone()
            if not existing:
                conn.execute(
                    sa.text(
                        "INSERT INTO resident (name, nfc_code, room_number, active) "
                        "VALUES (:name, :nfc_code, :room_number, 1)"
                    ),
                    {'name': description or number, 'nfc_code': number, 'room_number': number},
                )


def downgrade():
    with op.batch_alter_table('resident', schema=None) as batch_op:
        batch_op.drop_constraint('fk_resident_group_id', type_='foreignkey')
        batch_op.drop_column('group_id')

    op.drop_table('cleaner_groups')
    op.drop_table('resident_group')
