"""
conftest.py — Fixtures compartidas para toda la suite de tests.

Estrategia:
- Se crea una base de datos SQLite temporal (fichero en disco) para tests.
- Se intercambia el engine cacheado por Flask-SQLAlchemy 3.x apuntando a
  esa BD temporal, de modo que la BD de producción nunca se toca.
- Cada test recibe su propia limpieza de datos (truncate) para garantizar
  aislamiento total entre tests.
"""

import os
import tempfile
import weakref

import pytest
import sqlalchemy as sa
from datetime import datetime, timedelta

from app import app as flask_app, db as _db, limiter as _limiter
from app.models import Cleaner, Room, RoomType, Floor, CleaningRecord


# ── Configuración de la aplicación de test ────────────────────────────────────

@pytest.fixture(scope="session")
def app():
    """
    Instancia Flask configurada para tests con BD SQLite temporal.

    Flask-SQLAlchemy 3.x cachea el engine en _app_engines[app][None].
    Se intercambia ese engine por uno que apunta a un fichero temporal,
    de modo que la BD de producción no se toca en ningún momento.
    Al terminar la sesión se restaura el engine original.
    """
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="test-secret-key",
        JWT_SECRET_KEY="test-jwt-secret-key",
        LOGIN_DISABLED=False,
    )

    # Crear fichero temporal para la BD de tests
    db_fd, db_path = tempfile.mkstemp(suffix=".test.db")
    os.close(db_fd)

    # Crear engine apuntando al fichero temporal
    test_engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    # Guardar el engine original y sustituirlo en la caché
    engines_cache = flask_app.extensions["sqlalchemy"]._app_engines
    original_engine_map = dict(engines_cache.get(flask_app, {}))
    engines_cache[flask_app] = {None: test_engine}

    # Flask reutiliza el contexto de aplicacion que abrimos aqui debajo en lugar
    # de crear uno por peticion, y Flask-Login guarda el usuario resuelto en `g`,
    # que vive en ese contexto. Resultado: en cuanto un test hacia login, TODAS
    # las peticiones posteriores del mismo test se atendian como ese usuario,
    # aunque vinieran de un cliente sin cookies. Los tests que comprueban que una
    # ruta exige administrador pasaban sin comprobar nada.
    #
    # Esto vacia el usuario cacheado antes de cada peticion, que es lo que ocurre
    # en produccion, donde cada peticion estrena contexto.
    @flask_app.before_request
    def _olvidar_usuario_cacheado():
        from flask import g
        g.pop('_login_user', None)

    with flask_app.app_context():
        _db.create_all()          # crea tablas en el fichero temporal
        yield flask_app
        _db.drop_all()            # limpia el fichero temporal

    # Restaurar el engine original
    engines_cache[flask_app] = original_engine_map
    test_engine.dispose()
    os.unlink(db_path)


@pytest.fixture(scope="function", autouse=True)
def reset_rate_limiter():
    """
    Vacía los contadores de Flask-Limiter antes de cada test.

    El login de admin y el de trabajadora están limitados a 10/minuto y el
    almacenamiento es en memoria (`storage_uri="memory://"`), así que sin esto
    la 11ª llamada a `auth_client` de la suite recibe un 429 y todos los tests
    posteriores fallan por falta de sesión. `RATELIMIT_ENABLED=False` no
    funciona una vez la app está construida.
    """
    try:
        _limiter.reset()
    except (AttributeError, NotImplementedError):  # pragma: no cover
        pass
    yield


def _truncate_all(db):
    """Elimina todos los datos de todas las tablas en orden seguro."""
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()


@pytest.fixture(scope="function")
def db(app):
    """
    Proporciona una sesión de BD limpia para cada test.

    Limpia los datos ANTES de cada test (para empezar desde cero)
    y DESPUÉS (para dejar la BD temporal en estado limpio).
    La BD de producción no se ve afectada en ningún momento.
    """
    with app.app_context():
        _truncate_all(_db)
        yield _db
        _truncate_all(_db)


@pytest.fixture(scope="function")
def client(app, db):
    """Cliente de test HTTP de Flask."""
    return app.test_client()


# ── Fixtures de datos ─────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def admin_user(db):
    """Limpiador con permisos de administrador."""
    user = Cleaner(username="admin", name="Administrador Test", is_admin=True)
    user.set_password("admin123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(scope="function")
def cleaner_user(db):
    """Limpiador sin permisos de administrador."""
    user = Cleaner(username="limpiadora1", name="Maria García", is_admin=False)
    user.set_password("limpia123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(scope="function")
def floor(db):
    """Planta de prueba."""
    floor = Floor(name="Planta Baja")
    db.session.add(floor)
    db.session.commit()
    return floor


@pytest.fixture(scope="function")
def room_type(db):
    """Tipo de espacio de prueba."""
    rt = RoomType(name="Habitación Individual")
    db.session.add(rt)
    db.session.commit()
    return rt


@pytest.fixture(scope="function")
def room(db, floor, room_type):
    """Habitación de prueba con planta y tipo de espacio asociados."""
    r = Room(
        number="101",
        floor_id=floor.id,
        room_type_id=room_type.id,
        description="Habitación de prueba",
    )
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture(scope="function")
def second_room(db, floor, room_type):
    """Segunda habitación para tests que requieren múltiples habitaciones."""
    r = Room(
        number="102",
        floor_id=floor.id,
        room_type_id=room_type.id,
        description="Segunda habitación de prueba",
    )
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture(scope="function")
def completed_record(db, cleaner_user, room):
    """
    Registro de limpieza completado (con start_time y end_time).
    Duración: 30 minutos.
    """
    start = datetime(2026, 4, 24, 9, 0, 0)
    end = start + timedelta(minutes=30)
    record = CleaningRecord(
        cleaner_id=cleaner_user.id,
        room_id=room.id,
        start_time=start,
        end_time=end,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture(scope="function")
def active_record(db, cleaner_user, room):
    """Registro de limpieza en curso (sin end_time).

    Empieza diez minutos antes, no ahora mismo: una sesión que de verdad está
    en curso lleva un rato abierta, y desde que existe la duración mínima una
    recién abierta no se puede cerrar. Los tests de cierre usan esta fixture.
    """
    record = CleaningRecord(
        cleaner_id=cleaner_user.id,
        room_id=room.id,
        start_time=datetime.now() - timedelta(minutes=10),
        end_time=None,
    )
    db.session.add(record)
    db.session.commit()
    return record


# ── Helper: sesión de admin autenticada ──────────────────────────────────────

@pytest.fixture(scope="function")
def auth_client(client, admin_user):
    """
    Cliente HTTP con sesión de administrador ya iniciada.
    Evita repetir el login en cada test que requiere autenticación web.
    """
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )
    return client
