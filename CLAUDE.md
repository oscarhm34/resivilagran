# NFC — La Vila Gran

Sistema de gestión para residencia de tercera edad: registro de limpiezas y
atenciones vía NFC, cuadrantes de turnos, residentes, medicación, valoraciones,
incidencias y actividades.

**Stack:** Flask 3 + SQLAlchemy 2 + Jinja2 + Bootstrap 5.3.3 · PostgreSQL 15 en
producción (SQLite en local/tests) · Docker en NAS Synology.

## Comandos

```
pytest                 # tests
flask db migrate -m "add ..." && flask db upgrade
python run.py          # dev en :5001
```

## Reglas del proyecto

@.claude/rules/00-arquitectura.md
@.claude/rules/01-backend-flask.md
@.claude/rules/02-modelos-migraciones.md
@.claude/rules/03-ui-templates.md
@.claude/rules/04-seguridad.md
@.claude/rules/05-tests.md
@.claude/rules/06-deploy-nas.md
@.claude/rules/07-documentacion.md

## Recordatorios de alto nivel

- Todo el texto visible de la app va **en castellano**.
- Ninguna ruta sin `@admin_required` o `@jwt_required()`.
- Commits a BD con `_safe_commit()`, no `db.session.commit()` directo.
- Funcionalidad nueva ⇒ actualizar `app/templates/admin_help.html` (doc + changelog).
- En el NAS: `flask db stamp`, **nunca** `flask db upgrade`.
