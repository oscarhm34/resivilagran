# Backend Flask — rutas, commits y consultas

## Cabecera de módulo

Todos los módulos de `app/` empiezan igual:

```python
"""Descripción corta del blueprint."""
from __future__ import annotations
```

## Protección de rutas — obligatorio

**Ninguna ruta se queda sin decorador de autorización.** Hay dos mundos:

| Consumidor | Decorador | Identidad |
|---|---|---|
| Panel admin (web, Jinja2) | `@admin_required` (de `app/utils.py`) | `current_user` (Flask-Login) |
| App/PWA de trabajadoras | `@jwt_required()` | `get_jwt_identity()` → `username` |

- `@admin_required` ya incluye `@login_required` y comprueba `current_user.is_admin`.
- Para páginas de solo lectura accesibles a cualquier usuario logueado, `@login_required`.
- En rutas JWT que reciben un `worker_id` del cliente, **validarlo siempre** con
  `_verify_worker_id(worker_id)` — nunca confiar en el ID que envía el cliente.
- Endpoints de login o de escritura sensible llevan `@limiter.limit("10/minute", methods=["POST"])`.

## Commits a base de datos

Usar **siempre** `_safe_commit()` de `app/utils.py` en vez de `db.session.commit()` directo:

```python
ok, error = _safe_commit('Error al guardar el registro de limpieza')
if not ok:
    return jsonify({'error': error}), 500
```

Hace rollback y traduce `IntegrityError` / `OperationalError` / `SQLAlchemyError` a
mensajes en castellano. `db.session.commit()` directo solo se acepta en comandos CLI
y scripts de migración puntuales.

## Respuestas

- Rutas `/api/...` y consumidores JWT → `jsonify({...}), <código>`.
- Rutas del panel admin → `render_template(...)` o `redirect(url_for(...))` + `flash(msg, categoría)`.
- Categorías de flash: `success`, `danger`, `warning`, `info`.
- Los mensajes de error al usuario van **en castellano** y sin detalles internos.
  El detalle técnico va a `app.logger.error('...: %s', e)` (formato `%s`, no f-string).

## Consultas

- **N+1:** cargar relaciones con `joinedload` / `subqueryload` en listados
  (`Room.query.options(joinedload(Room.floor), joinedload(Room.room_type))`).
- No usar `Model.query.get(id)` (deprecado en SQLAlchemy 2.x) para código nuevo;
  usar `db.session.get(Model, id)` o `filter_by(...).first()`. En el código hay
  una única aparición heredada en `app/__init__.py` (`load_user`).
- **Nunca** construir SQL con f-strings ni concatenación. Las únicas
  `cursor.execute()` legítimas son los PRAGMA de SQLite en `app/__init__.py`.
- Filtrar consultas de listados por rango de día con `_today_range()` en vez de
  comparar fechas a mano.

## Fechas y horas

El proyecto trabaja en **hora local Europe/Madrid** (`TZ` fijada en `docker-compose.yml`)
y usa `datetime.now()` de forma generalizada (~100 usos). Mantener `datetime.now()`
para timestamps de negocio; no mezclar con `datetime.utcnow()` en el mismo flujo.

## Helpers existentes — reutilizar, no duplicar

De `app/utils.py`:
`admin_required`, `_verify_worker_id`, `_safe_commit`, `_allowed_file`,
`_format_duration`, `_today_range`, `_resolve_nfc_code`,
`_check_single_session_conflict`, `_calculate_room_urgency`, `_urgency_priority`,
`_compute_cleaning_stats`, `log_audit`.

Antes de escribir un helper nuevo, comprobar que no existe ya ahí.

## Auditoría

Las operaciones administrativas que crean, modifican o borran datos deben registrar
`log_audit(action, table_name, record_id, details)` antes del commit.
