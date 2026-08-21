# Tests

## Ejecutar

```
pytest                    # toda la suite (testpaths = tests)
pytest tests/test_nfc_session.py -v
pytest -k "cleaning"
```

## Aislamiento de la base de datos — no romperlo

`tests/conftest.py` crea una **SQLite temporal en fichero** y sustituye el engine
cacheado por Flask-SQLAlchemy 3.x (`app.extensions["sqlalchemy"]._app_engines`),
restaurándolo al terminar la sesión. Es un mecanismo frágil pero deliberado: la BD de
producción nunca se toca.

Reglas derivadas:

- **Ningún test escribe en `instance/cleaning_service.db`** ni depende de datos
  preexistentes.
- Cada test crea sus propios datos mediante fixtures; nada de estado compartido entre
  tests ni de dependencia del orden de ejecución.
- Si se toca `conftest.py`, ejecutar la suite completa después: un fallo ahí puede
  hacer que los tests apunten a la BD real.

## Configuración de test

La app de test fija `TESTING=True`, `WTF_CSRF_ENABLED=False` y claves de test.
No añadir a los tests dependencia de `.env` ni de red.

## Qué cubrir al añadir funcionalidad

Ficheros existentes: `test_api.py`, `test_auth.py`, `test_models.py`,
`test_records.py`, `test_rooms.py`, `test_workers.py`, `test_nfc_session.py`.

Para una ruta nueva, como mínimo:

1. **Autorización** — sin token / sin admin devuelve 401/403. Es el test más
   importante: hay 230 rutas y la protección es manual.
2. **Camino correcto** — respuesta y efecto en BD.
3. **Entrada inválida** — códigos NFC inexistentes, IDs de otro trabajador, fechas
   mal formadas, campos que faltan.
4. **Cruce de identidad** — que una trabajadora no pueda operar sobre registros de
   otra (`_verify_worker_id`).

Para lógica de negocio con reglas (turnos en `scheduler.py`, urgencia de limpieza en
`_calculate_room_urgency`, duraciones), test unitario directo de la función con los
casos límite, sin pasar por HTTP.

## Estilo

- AAA (arrange / act / assert), un comportamiento por test.
- Nombres descriptivos en castellano o inglés, coherentes con el fichero donde van.
- Aserciones concretas: comprobar el valor, no solo que la respuesta sea 200.
