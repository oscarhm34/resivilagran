# Modelos y migraciones

## Convenciones de modelo

Todos los modelos viven en `app/models.py`.

```python
class MiModelo(db.Model):
    __tablename__ = 'mi_modelo'          # snake_case explícito
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'),
                           nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
```

- **`nullable=` siempre explícito.** No dejarlo al criterio por defecto.
- **`index=True`** en toda FK y en toda columna que se filtre u ordene con frecuencia
  (`start_time`, `date`, `last_active`).
- Relaciones con `back_populates` (bidireccional explícito) o `backref` cuando ya se
  usa así en el modelo vecino. Ser coherente con el modelo relacionado.
- Métodos de negocio pequeños dentro del modelo (`calculate_duration`,
  `check_password`, `current_year_records` como `@classmethod`).
- Contraseñas: `set_password()` / `check_password()` con Werkzeug. Nunca guardar
  ni loguear la contraseña en claro.

## Doble motor: SQLite y PostgreSQL

Producción usa **PostgreSQL 15** (`DATABASE_URL`); desarrollo y tests caen a SQLite
(`sqlite:///cleaning_service.db`). Por tanto:

- Nada específico de un motor en modelos ni consultas (sin `json_extract`, sin
  `ON CONFLICT`, sin tipos propietarios).
- Los PRAGMA de SQLite en `app/__init__.py` van protegidos con
  `isinstance(dbapi_conn, sqlite3.Connection)` — no romper esa guarda.
- Cuidado con `extract('year', ...)` y agregaciones: verificar que funcionan en ambos.

## Migraciones Alembic

Toda columna o tabla nueva necesita migración:

```
flask db migrate -m "add <descripción>"
flask db upgrade
```

- Revisar el fichero generado en `migrations/versions/` antes de commitear: Alembic
  detecta mal los `ALTER` de SQLite y a veces genera drops no deseados.
- Nombre del mensaje en inglés y descriptivo, siguiendo el estilo existente
  (`add_medication_tables`, `add_indexes_for_performance`).
- Para nombres de constraint en FKs nuevas, darlos explícitos
  (`db.ForeignKey('floor.id', name='fk_floor_id')`) — SQLite lo necesita para poder
  alterarlas después.

## En producción (NAS): `stamp`, nunca `upgrade`

El contenedor crea las tablas al arrancar (`db.create_all()` en `run.py`), así que
`flask db upgrade` en el NAS **siempre falla** con `table already exists`.
Ver [06-deploy-nas.md](06-deploy-nas.md) para el procedimiento correcto.
