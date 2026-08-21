# Arquitectura y organización del código

Sistema de gestión para la residencia de tercera edad **La Vila Gran**: registro de
limpiezas y atenciones vía NFC, cuadrantes de turnos, residentes, medicación,
valoraciones, incidencias y actividades.

## Estructura

```
app/
  __init__.py          # Instancia Flask global + extensiones + registro de blueprints
  config.py            # class Config — secretos persistentes, VAPID, rutas
  models.py            # TODOS los modelos SQLAlchemy (~884 líneas)
  utils.py             # Helpers compartidos entre blueprints
  routes.py            # teardown + comandos CLI (flask create-admin, init-admin…)
  scheduler.py         # SmartScheduler — generación de cuadrantes en 5 fases
  chatbot.py           # Integración Anthropic (claude-haiku-4-5-20251001)
  ml_models.py         # Predicciones/estadísticas
  resiplus_client.py   # Cliente SOAP Resiplus (integración externa)
  blueprints/          # 14 blueprints por dominio funcional
  templates/           # Jinja2 — todos extienden base.html excepto worker.html
migrations/versions/   # Alembic
tests/                 # pytest + conftest.py con BD temporal
```

## Dónde va cada cosa

- **Ruta nueva** → al blueprint del dominio correspondiente
  (`cleaning`, `nfc`, `admin`, `shifts`, `residents`, `care`, `incidents`,
  `medication`, `assessments`, `activities`, `notifications`, `training`,
  `documents`, `chat`). **No** añadir rutas a `app/routes.py`: ese fichero es solo
  para CLI y teardown.
- **Lógica reutilizada por 2+ blueprints** → `app/utils.py`, con prefijo `_` si es
  interna (`_safe_commit`, `_resolve_nfc_code`, `_format_duration`).
- **Modelo nuevo** → `app/models.py` (fichero único, no dividir) + migración Alembic.
- **Comando de mantenimiento** → `@app.cli.command` en `app/routes.py`.

## Blueprint nuevo

1. Crear `app/blueprints/<nombre>.py` con docstring de módulo y `bp = Blueprint('<nombre>', __name__)`.
2. Importar y registrar en `app/__init__.py` (imports con `# noqa: E402`).
3. Añadirlo a la lista de `csrf.exempt(...)` al final de `app/__init__.py`.

## Patrón de instancia

La app usa una **instancia Flask global** (`app = Flask(__name__)` en `app/__init__.py`),
no un application factory. Los blueprints importan `from .. import db, app, limiter`.
No refactorizar a factory sin acuerdo explícito: los tests (`conftest.py`) dependen de
sustituir el engine cacheado de esta instancia.

## Fuera del árbol de la app

`MainActivity1.kt`, `NfcActivity.kt`, `CleaningUtils.kt` son restos de la app Android
original en la raíz del repo — no forman parte del build de Flask. Los `.apk` de
`app/static/` son builds antiguos: no añadir más ahí.
