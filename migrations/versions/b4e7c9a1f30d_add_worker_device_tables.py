"""add worker device tables

Revision ID: b4e7c9a1f30d
Revises: a7f1c4e28d53
Create Date: 2026-09-16

Saber desde que movil trabaja cada una.

Hasta ahora el inicio de sesion de la webapp no dejaba rastro: validaba la
contrasena, devolvia el token y se olvidaba. Lo unico que quedaba era
`cleaner.last_active`, que dice cuando pero no desde donde. Cuando a alguien no
le funcionaba el NFC no habia manera de saber que telefono habia que mirar.

El User-Agent por si solo no vale: en la residencia hay varios moviles del mismo
modelo y todos dicen exactamente lo mismo. La IP tampoco, porque la red va por
DHCP y cambia. Por eso `worker_device` se identifica por `device_uid`, un
identificador que genera el navegador la primera vez y guarda en localStorage, y
lleva un `label` que pone coordinacion a mano ("Movil planta 1"): es lo unico que
permite senalar un telefono concreto entre cinco iguales.

`worker_device_use` es una fila por pareja (movil, trabajadora), actualizada en
su sitio. No es un historial de inicios de sesion: es el estado de quien usa que.
Un movil compartido entre el turno de manana y el de noche tiene dos filas, no
doscientas.

Las dos son tablas NUEVAS, asi que `db.create_all()` las crea sola al arrancar el
contenedor del NAS y no hace falta ningun ALTER manual. Basta con el
`flask db stamp` de siempre. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b4e7c9a1f30d'
down_revision = 'a7f1c4e28d53'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'worker_device',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('device_uid', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=60), nullable=True),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('model', sa.String(length=80), nullable=True),
        sa.Column('os_name', sa.String(length=40), nullable=True),
        sa.Column('browser', sa.String(length=40), nullable=True),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_uid'),
    )
    with op.batch_alter_table('worker_device', schema=None) as batch_op:
        batch_op.create_index('ix_worker_device_device_uid', ['device_uid'], unique=True)
        batch_op.create_index('ix_worker_device_last_seen', ['last_seen'], unique=False)

    op.create_table(
        'worker_device_use',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('device_id', sa.Integer(), nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=False),
        sa.Column('last_login', sa.DateTime(), nullable=False),
        sa.Column('login_count', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['device_id'], ['worker_device.id'],
                                name='fk_wdu_device', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['worker_id'], ['cleaner.id'], name='fk_wdu_cleaner'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_id', 'worker_id', name='uq_wdu_device_worker'),
    )
    with op.batch_alter_table('worker_device_use', schema=None) as batch_op:
        batch_op.create_index('ix_worker_device_use_device_id', ['device_id'], unique=False)
        batch_op.create_index('ix_worker_device_use_worker_id', ['worker_id'], unique=False)
        batch_op.create_index('ix_worker_device_use_last_login', ['last_login'], unique=False)


def downgrade():
    with op.batch_alter_table('worker_device_use', schema=None) as batch_op:
        batch_op.drop_index('ix_worker_device_use_last_login')
        batch_op.drop_index('ix_worker_device_use_worker_id')
        batch_op.drop_index('ix_worker_device_use_device_id')
    op.drop_table('worker_device_use')

    with op.batch_alter_table('worker_device', schema=None) as batch_op:
        batch_op.drop_index('ix_worker_device_last_seen')
        batch_op.drop_index('ix_worker_device_device_uid')
    op.drop_table('worker_device')
