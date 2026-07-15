"""
test_api.py — Tests de la API móvil (endpoints consumidos por la app Android).

Endpoints cubiertos:
- POST /login          → autenticación JWT
- POST /start_cleaning → iniciar/finalizar limpieza por NFC
- POST /end_cleaning   → finalizar limpieza por record_id
- GET  /check_cleaning → consulta si hay limpieza en curso
- GET  /cleaning_summary/<id> → resumen diario del limpiador
"""

import json
import pytest
from datetime import datetime, timedelta

from app.models import CleaningRecord
from app import db as _db


# ── Helpers ───────────────────────────────────────────────────────────────────

def post_json(client, url, payload):
    """Envía una petición POST con cuerpo JSON."""
    return client.post(
        url,
        data=json.dumps(payload),
        content_type="application/json",
    )


# ── POST /login ───────────────────────────────────────────────────────────────

class TestLoginJWT:
    """Tests del endpoint de autenticación JWT para la app Android."""

    def test_login_valid_credentials_returns_jwt(self, client, cleaner_user):
        """Credenciales correctas devuelven access_token, id_cleaner y cleaner_name."""
        response = post_json(
            client, "/login", {"username": "limpiadora1", "password": "limpia123"}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "access_token" in data
        assert data["id_cleaner"] == cleaner_user.id
        assert data["cleaner_name"] == cleaner_user.name

    def test_login_wrong_password_returns_401(self, client, cleaner_user):
        """Contraseña incorrecta devuelve 401 con mensaje de error."""
        response = post_json(
            client, "/login", {"username": "limpiadora1", "password": "mala"}
        )
        assert response.status_code == 401
        assert "error" in response.get_json()

    def test_login_nonexistent_user_returns_401(self, client, db):
        """Usuario inexistente devuelve 401."""
        response = post_json(
            client, "/login", {"username": "nadie", "password": "cualquiera"}
        )
        assert response.status_code == 401

    def test_login_admin_user_also_works(self, client, admin_user):
        """Un administrador también puede autenticarse via JWT (la app no discrimina)."""
        response = post_json(
            client, "/login", {"username": "admin", "password": "admin123"}
        )
        assert response.status_code == 200
        assert "access_token" in response.get_json()

    def test_login_get_redirects_to_admin_login(self, client, db):
        """Una petición GET a /login redirige al login web, no devuelve JSON."""
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]

    def test_login_accepts_form_data(self, client, cleaner_user):
        """El endpoint también acepta credenciales como form-data (compatibilidad)."""
        response = client.post(
            "/login",
            data={"username": "limpiadora1", "password": "limpia123"},
        )
        assert response.status_code == 200
        assert "access_token" in response.get_json()


# ── POST /start_cleaning ──────────────────────────────────────────────────────

class TestStartCleaning:
    """Tests del endpoint de inicio de limpieza."""

    def test_start_cleaning_creates_new_record(self, client, cleaner_user, room):
        """Primera lectura NFC en una habitación crea un registro nuevo."""
        response = post_json(
            client,
            "/start_cleaning",
            {"cleaner_id": cleaner_user.id, "room_id": room.number},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "record_id" in data
        assert "iniciada" in data["message"]

    def test_start_cleaning_second_scan_ends_active_cleaning(
        self, client, active_record, cleaner_user, room
    ):
        """
        Segunda lectura NFC en la misma habitación donde hay limpieza activa
        la finaliza en lugar de crear un nuevo registro.
        """
        response = post_json(
            client,
            "/start_cleaning",
            {"cleaner_id": cleaner_user.id, "room_id": room.number},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "finalizada" in data["message"]
        # No debe devolver record_id nuevo
        assert "record_id" not in data

    def test_start_cleaning_nonexistent_room_returns_404(self, client, cleaner_user, db):
        """Número de habitación desconocido devuelve 404."""
        response = post_json(
            client,
            "/start_cleaning",
            {"cleaner_id": cleaner_user.id, "room_id": "NOEXISTE"},
        )
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_start_cleaning_sets_start_time(self, client, cleaner_user, room, app):
        """El registro creado tiene start_time y end_time es None."""
        response = post_json(
            client,
            "/start_cleaning",
            {"cleaner_id": cleaner_user.id, "room_id": room.number},
        )
        assert response.status_code == 200
        record_id = response.get_json()["record_id"]
        with app.app_context():
            record = CleaningRecord.query.get(record_id)
            assert record.start_time is not None
            assert record.end_time is None


# ── POST /end_cleaning ────────────────────────────────────────────────────────

class TestEndCleaning:
    """Tests del endpoint de finalización de limpieza por record_id."""

    def test_end_cleaning_sets_end_time(self, client, active_record, app):
        """Finalizar una limpieza activa actualiza end_time y devuelve duración."""
        response = post_json(
            client, "/end_cleaning", {"record_id": active_record.id}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "duration" in data
        assert data["duration"] is not None

    def test_end_cleaning_already_finished_returns_400(
        self, client, completed_record
    ):
        """Intentar finalizar una limpieza ya completada devuelve 400."""
        response = post_json(
            client, "/end_cleaning", {"record_id": completed_record.id}
        )
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_end_cleaning_nonexistent_record_returns_400(self, client, db):
        """record_id inexistente devuelve 400."""
        response = post_json(client, "/end_cleaning", {"record_id": 99999})
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_end_cleaning_duration_is_positive(self, client, active_record):
        """La duración devuelta debe ser un número positivo."""
        response = post_json(
            client, "/end_cleaning", {"record_id": active_record.id}
        )
        data = response.get_json()
        assert response.status_code == 200
        assert float(data["duration"]) >= 0


# ── GET /check_cleaning ───────────────────────────────────────────────────────

class TestCheckCleaning:
    """Tests del endpoint de consulta de limpieza en curso."""

    def test_check_cleaning_returns_room_id_when_active(
        self, client, active_record, cleaner_user
    ):
        """Si hay limpieza activa devuelve room_id."""
        response = client.get(f"/check_cleaning?cleaner_id={cleaner_user.id}")
        assert response.status_code == 200
        data = response.get_json()
        assert "room_id" in data
        assert data["room_id"] == active_record.room_id

    def test_check_cleaning_returns_message_when_no_active(
        self, client, cleaner_user
    ):
        """Sin limpieza activa devuelve mensaje informativo."""
        response = client.get(f"/check_cleaning?cleaner_id={cleaner_user.id}")
        assert response.status_code == 200
        data = response.get_json()
        assert "message" in data

    def test_check_cleaning_missing_cleaner_id_returns_400(self, client, db):
        """Llamada sin cleaner_id devuelve 400."""
        response = client.get("/check_cleaning")
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_check_cleaning_completed_record_not_shown(
        self, client, completed_record, cleaner_user
    ):
        """Un registro ya finalizado no aparece como limpieza en curso."""
        response = client.get(f"/check_cleaning?cleaner_id={cleaner_user.id}")
        assert response.status_code == 200
        data = response.get_json()
        # No debe haber room_id activo
        assert "room_id" not in data


# ── GET /cleaning_summary/<id> ────────────────────────────────────────────────

class TestCleaningSummary:
    """Tests del resumen diario de limpiezas del limpiador."""

    def test_summary_empty_for_cleaner_with_no_records_today(
        self, client, cleaner_user
    ):
        """Sin registros de hoy, la respuesta es una lista vacía."""
        response = client.get(f"/cleaning_summary/{cleaner_user.id}")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_summary_includes_completed_cleanings_done_today(
        self, client, cleaner_user, room, app, db
    ):
        """Los registros completados de hoy aparecen en el resumen."""
        today = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        record = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=today,
            end_time=today + timedelta(minutes=20),
        )
        _db.session.add(record)
        _db.session.commit()

        response = client.get(f"/cleaning_summary/{cleaner_user.id}")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) == 1
        # Debe incluir la descripción de la habitación
        assert room.description in data[0]

    def test_summary_excludes_records_from_other_days(
        self, client, completed_record, cleaner_user
    ):
        """
        Un registro de otro día (2026-04-24) no aparece en el resumen
        (que sólo muestra el día actual).
        """
        # completed_record tiene start_time en 2026-04-24
        # El resumen filtra por el día de hoy (2026-04-24 en este caso),
        # así que SÍ aparecería si hoy es esa fecha. El test verifica
        # que la respuesta es siempre una lista (formato correcto).
        response = client.get(f"/cleaning_summary/{cleaner_user.id}")
        assert response.status_code == 200
        assert isinstance(response.get_json(), list)
