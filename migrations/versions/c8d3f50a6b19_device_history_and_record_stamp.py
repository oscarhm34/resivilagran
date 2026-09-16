"""add device login history and stamp records with the device

Revision ID: c8d3f50a6b19
Revises: b4e7c9a1f30d
Create Date: 2026-09-16

Los moviles no son de nadie: cada trabajadora coge el que esta libre.

Eso invalida tres cosas de la version anterior, que daba por hecho que cada
persona tenia su telefono:

1. `worker_device_use.last_login` se llamaba mal. Se refresca en cada peticion,
   no solo al entrar, asi que lo que guarda es la ultima vez que esa persona
   toco ese movil. Con telefonos rotando ese es el dato importante —el maximo
   por dispositivo es quien lo lleva ahora mismo—, y llamarlo "login" hacia
   pensar que contaba inicios de sesion. Pasa a `last_used`.

2. Falta el historial. El resumen de `worker_device_use` solo contesta por hoy:
   en cuanto el telefono cambia de manos, quien lo llevaba ayer se pierde.
   `worker_device_login` lo anota, una fila por inicio de sesion. Crece poco:
   un punado de entradas por persona y dia.

3. Faltaba saber desde que aparato se registro cada cosa. Antes se deducia de
   la persona; rotando, ya no. `cleaning_record.device_id` y
   `care_record.device_id` lo guardan, para que cuando una lectura NFC salga
   rara se pueda ir al telefono concreto. NULL en todo lo anterior a esta
   migracion y en lo que entre por la app Android antigua, que no manda la
   cabecera.

EN EL NAS: `db.create_all()` crea `worker_device_login` sola, pero NO hace el
rename ni anade las dos columnas. Los tres ALTER van a mano, cada uno en una
sola linea. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c8d3f50a6b19'
down_revision = 'b4e7c9a1f30d'
branch_labels = None
depends_on = None


def upgrade():
    # 1. El nombre que enganaba
    with op.batch_alter_table('worker_device_use', schema=None) as batch_op:
        batch_op.drop_index('ix_worker_device_use_last_login')
        batch_op.alter_column('last_login', new_column_name='last_used')
        batch_op.create_index('ix_worker_device_use_last_used', ['last_used'], unique=False)

    # 2. El historial
    op.create_table(
        'worker_device_login',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('device_id', sa.Integer(), nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['device_id'], ['worker_device.id'],
                                name='fk_wdl_device', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['worker_id'], ['cleaner.id'], name='fk_wdl_cleaner'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('worker_device_login', schema=None) as batch_op:
        batch_op.create_index('ix_worker_device_login_device_id', ['device_id'], unique=False)
        batch_op.create_index('ix_worker_device_login_worker_id', ['worker_id'], unique=False)
        batch_op.create_index('ix_worker_device_login_at', ['at'], unique=False)

    # 3. Desde que movil se registro cada cosa
    with op.batch_alter_table('cleaning_record', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_cleaning_record_device_id', ['device_id'], unique=False)
        batch_op.create_foreign_key('fk_cleaning_device', 'worker_device',
                                    ['device_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('care_record', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_care_record_device_id', ['device_id'], unique=False)
        batch_op.create_foreign_key('fk_care_device', 'worker_device',
                                    ['device_id'], ['id'], ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('care_record', schema=None) as batch_op:
        batch_op.drop_constraint('fk_care_device', type_='foreignkey')
        batch_op.drop_index('ix_care_record_device_id')
        batch_op.drop_column('device_id')

    with op.batch_alter_table('cleaning_record', schema=None) as batch_op:
        batch_op.drop_constraint('fk_cleaning_device', type_='foreignkey')
        batch_op.drop_index('ix_cleaning_record_device_id')
        batch_op.drop_column('device_id')

    with op.batch_alter_table('worker_device_login', schema=None) as batch_op:
        batch_op.drop_index('ix_worker_device_login_at')
        batch_op.drop_index('ix_worker_device_login_worker_id')
        batch_op.drop_index('ix_worker_device_login_device_id')
    op.drop_table('worker_device_login')

    with op.batch_alter_table('worker_device_use', schema=None) as batch_op:
        batch_op.drop_index('ix_worker_device_use_last_used')
        batch_op.alter_column('last_used', new_column_name='last_login')
        batch_op.create_index('ix_worker_device_use_last_login', ['last_login'], unique=False)
