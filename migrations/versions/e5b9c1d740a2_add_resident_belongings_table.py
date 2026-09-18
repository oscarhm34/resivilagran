"""add resident belongings table

Revision ID: e5b9c1d740a2
Revises: d1a4f76b2c08
Create Date: 2026-09-18

Inventario de pertenencias: una fila por objeto personal del residente (ropa,
maquinillas, panuelos, peines, cinturones), con una foto opcional guardada en
`uploads/belongings/res_<id>/` y una descripcion. Hasta ahora no habia registro
de lo que trae cada residente, asi que cuando una prenda se perdia nadie podia
decir si llego o no.

`photo_path` y `description` son las dos nullable a proposito: un objeto puede
ir solo con foto o solo con texto. Que no esten las dos vacias se comprueba en
la ruta, no con un CHECK, para no depender del motor.

EN EL NAS: `db.create_all()` ya crea las tablas nuevas al arrancar, asi que
`flask db upgrade` falla con `table already exists`. Va `flask db stamp
e5b9c1d740a2`. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5b9c1d740a2'
down_revision = 'd1a4f76b2c08'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'resident_belonging',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('resident_id', sa.Integer(), nullable=False),
        sa.Column('photo_path', sa.String(length=255), nullable=True),
        sa.Column('category', sa.String(length=20), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['resident_id'], ['resident.id'],
                                name='fk_belonging_resident_id'),
        sa.ForeignKeyConstraint(['created_by'], ['cleaner.id'],
                                name='fk_belonging_cleaner_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('resident_belonging') as batch_op:
        batch_op.create_index('ix_resident_belonging_resident_id', ['resident_id'])
        batch_op.create_index('ix_resident_belonging_category', ['category'])
        batch_op.create_index('ix_resident_belonging_created_at', ['created_at'])


def downgrade():
    # Las fotos de uploads/belongings/ no se tocan: si se vuelve atras por un
    # fallo de despliegue, borrarlas seria irreversible de verdad.
    with op.batch_alter_table('resident_belonging') as batch_op:
        batch_op.drop_index('ix_resident_belonging_created_at')
        batch_op.drop_index('ix_resident_belonging_category')
        batch_op.drop_index('ix_resident_belonging_resident_id')
    op.drop_table('resident_belonging')
