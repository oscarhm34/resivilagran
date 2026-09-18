"""belonging photos to own table

Revision ID: a92d47f1c3e8
Revises: f3c81e560b47
Create Date: 2026-09-18

Una pertenencia pasa de tener una foto a tener varias. Un jersey se reconoce
mucho mejor con el conjunto, la etiqueta de la talla y el remiendo del puno
que con una sola imagen de frente, y para identificar un objeto perdido eso
se nota: al comparar gana la mejor foto de cada objeto.

Las cuatro columnas de foto y descriptores salen de `resident_belonging` y se
van a `belonging_photo`, una fila por foto. Los datos que ya hubiera se copian
antes de borrar las columnas: los ficheros de `uploads/belongings/` no se tocan
en ningun momento, solo cambia la fila que los apunta.

EN EL NAS: `db.create_all()` crea `belonging_photo` al arrancar, pero NO copia
las fotos que ya estaban ni borra las columnas viejas. El paso manual es
`flask migrar-fotos-inventario`, que hace la copia y es idempotente. Borrar las
columnas viejas es opcional y va aparte. Ver .claude/rules/06-deploy-nas.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a92d47f1c3e8'
down_revision = 'f3c81e560b47'
branch_labels = None
depends_on = None


def upgrade():
    # El orden importa, y no es el evidente. En SQLite quitar columnas obliga a
    # recrear la tabla entera (crear, copiar, DROP, renombrar), y ese DROP falla
    # si ya existe `belonging_photo` apuntandola con una clave ajena. Asi que
    # los datos se aparcan en una tabla sin ataduras, se limpia
    # `resident_belonging`, y solo entonces nace la tabla definitiva.
    op.create_table(
        '_belonging_photo_tmp',
        sa.Column('belonging_id', sa.Integer(), nullable=False),
        sa.Column('photo_path', sa.String(length=255), nullable=False),
        sa.Column('embedding', sa.LargeBinary(), nullable=True),
        sa.Column('color_hist', sa.LargeBinary(), nullable=True),
        sa.Column('descriptor_version', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.execute("""
        INSERT INTO _belonging_photo_tmp
            (belonging_id, photo_path, embedding, color_hist, descriptor_version, created_at)
        SELECT id, photo_path, embedding, color_hist, descriptor_version, created_at
        FROM resident_belonging
        WHERE photo_path IS NOT NULL
    """)

    with op.batch_alter_table('resident_belonging') as batch_op:
        batch_op.drop_index('ix_resident_belonging_descriptor_version')
        batch_op.drop_column('descriptor_version')
        batch_op.drop_column('color_hist')
        batch_op.drop_column('embedding')
        batch_op.drop_column('photo_path')

    op.create_table(
        'belonging_photo',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('belonging_id', sa.Integer(), nullable=False),
        sa.Column('photo_path', sa.String(length=255), nullable=False),
        sa.Column('embedding', sa.LargeBinary(), nullable=True),
        sa.Column('color_hist', sa.LargeBinary(), nullable=True),
        sa.Column('descriptor_version', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['belonging_id'], ['resident_belonging.id'],
                                name='fk_photo_belonging_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('belonging_photo') as batch_op:
        batch_op.create_index('ix_belonging_photo_belonging_id', ['belonging_id'])
        batch_op.create_index('ix_belonging_photo_descriptor_version', ['descriptor_version'])

    op.execute("""
        INSERT INTO belonging_photo
            (belonging_id, photo_path, embedding, color_hist, descriptor_version, created_at)
        SELECT belonging_id, photo_path, embedding, color_hist, descriptor_version, created_at
        FROM _belonging_photo_tmp
    """)
    op.drop_table('_belonging_photo_tmp')


def downgrade():
    with op.batch_alter_table('resident_belonging') as batch_op:
        batch_op.add_column(sa.Column('photo_path', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('embedding', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('color_hist', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('descriptor_version', sa.String(length=40), nullable=True))
        batch_op.create_index('ix_resident_belonging_descriptor_version',
                              ['descriptor_version'])

    # Solo cabe una: se recupera la primera de cada objeto y las demas se
    # quedan sin fila que las apunte. Los ficheros siguen en uploads/.
    op.execute("""
        UPDATE resident_belonging SET
            photo_path = (SELECT p.photo_path FROM belonging_photo p
                          WHERE p.belonging_id = resident_belonging.id
                          ORDER BY p.id LIMIT 1),
            embedding = (SELECT p.embedding FROM belonging_photo p
                         WHERE p.belonging_id = resident_belonging.id
                         ORDER BY p.id LIMIT 1),
            color_hist = (SELECT p.color_hist FROM belonging_photo p
                          WHERE p.belonging_id = resident_belonging.id
                          ORDER BY p.id LIMIT 1),
            descriptor_version = (SELECT p.descriptor_version FROM belonging_photo p
                                  WHERE p.belonging_id = resident_belonging.id
                                  ORDER BY p.id LIMIT 1)
    """)

    with op.batch_alter_table('belonging_photo') as batch_op:
        batch_op.drop_index('ix_belonging_photo_descriptor_version')
        batch_op.drop_index('ix_belonging_photo_belonging_id')
    op.drop_table('belonging_photo')
