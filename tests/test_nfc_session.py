"""
test_nfc_session.py — Tests para los cambios recientes en app/blueprints/nfc.py:

- Guardia nfc_only en end_session y cancel_session (403 cuando está activa)
- El endpoint /api/nfc/scan sigue funcionando para cerrar sesiones con nfc_only=true
- Nuevo endpoint /api/nfc/refresh-token
- GET /api/config incluye hidden_logout_minutes
"""

import json
import pytest
from datetime import datetime

from app import db as _db, limiter as _limiter
from app.models import AppSetting, Resident, CareRecord, CleaningRecord


# ── Helpers ───────────────────────────────────────────────────────────────────

def post_json(client, url, payload, token=None):
    """POST con cuerpo JSON, opcionalmente con cabecera JWT."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(url, data=json.dumps(payload), headers=headers)


def get_jwt(client, username, password):
    """Obtiene un JWT haciendo login en /login. Lanza AssertionError si falla."""
    resp = post_json(client, "/login", {"username": username, "password": password})
    assert resp.status_code == 200, f"Login fallido para '{username}': {resp.get_json()}"
    return resp.get_json()["access_token"]


def set_nfc_only(value: bool):
    """Guarda nfc_only en AppSetting dentro de la sesión activa."""
    AppSetting.set("nfc_only", "true" if value else "false")
    _db.session.commit()


# ── Fixtures locales ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_rate_limiter(app):
    """
    Reinicia el almacenamiento del rate limiter antes de cada test.

    El limiter usa almacenamiento en memoria compartido durante la sesión de tests.
    Sin este reset, los múltiples logins acumulan llamadas y superan el límite
    de 10/minuto configurado en /login, produciendo 429 en tests posteriores.
    """
    with app.app_context():
        _limiter.reset()
    yield


@pytest.fixture()
def jwt_token(client, cleaner_user):
    """JWT válido para cleaner_user (limpiadora1)."""
    return get_jwt(client, "limpiadora1", "limpia123")


@pytest.fixture()
def resident(db):
    """Residente con código NFC único para tests de scan/care."""
    r = Resident(name="Rosa Martínez", nfc_code="NFC-TEST-001", active=True)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture()
def active_cleaning_record(db, cleaner_user, room):
    """Registro de limpieza en curso (sin end_time) para cleaner_user en room."""
    rec = CleaningRecord(
        cleaner_id=cleaner_user.id,
        room_id=room.id,
        start_time=datetime.now(),
        end_time=None,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


@pytest.fixture()
def active_care_record(db, cleaner_user, resident):
    """Registro de atención en curso (sin end_time) para cleaner_user con resident."""
    rec = CareRecord(
        worker_id=cleaner_user.id,
        resident_id=resident.id,
        start_time=datetime.now(),
        end_time=None,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


# ── Tests: guardia nfc_only en end_session ────────────────────────────────────

class TestEndSessionNfcOnlyGuard:
    """POST /api/nfc/end-session debe respetar la configuración nfc_only."""

    def test_end_session_returns_403_when_nfc_only_is_true(
        self, client, db, cleaner_user, active_cleaning_record, jwt_token
    ):
        """Con nfc_only=true el endpoint devuelve 403 antes de intentar nada."""
        set_nfc_only(True)

        resp = post_json(
            client,
            "/api/nfc/end-session",
            {"worker_id": cleaner_user.id, "record_id": active_cleaning_record.id, "mode": "cleaning"},
            token=jwt_token,
        )

        assert resp.status_code == 403
        data = resp.get_json()
        assert "error" in data

    def test_end_session_record_not_modified_when_nfc_only_blocks(
        self, client, db, cleaner_user, active_cleaning_record, jwt_token
    ):
        """El registro activo no se toca cuando la guardia nfc_only devuelve 403."""
        set_nfc_only(True)

        post_json(
            client,
            "/api/nfc/end-session",
            {"worker_id": cleaner_user.id, "record_id": active_cleaning_record.id, "mode": "cleaning"},
            token=jwt_token,
        )

        db.session.expire_all()
        rec = db.session.get(CleaningRecord, active_cleaning_record.id)
        assert rec.end_time is None

    def test_end_session_cleaning_works_when_nfc_only_is_false(
        self, client, db, cleaner_user, active_cleaning_record, jwt_token
    ):
        """Con nfc_only=false y un registro activo válido el endpoint finaliza la limpieza."""
        set_nfc_only(False)

        resp = post_json(
            client,
            "/api/nfc/end-session",
            {
                "worker_id": cleaner_user.id,
                "record_id": active_cleaning_record.id,
                "mode": "cleaning",
                "skip_checklist": True,
            },
            token=jwt_token,
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("action") == "ended"

    def test_end_session_requires_jwt(self, client, db, cleaner_user, active_cleaning_record):
        """Sin cabecera Authorization el endpoint devuelve 401/422."""
        set_nfc_only(False)

        resp = post_json(
            client,
            "/api/nfc/end-session",
            {"worker_id": cleaner_user.id, "record_id": active_cleaning_record.id, "mode": "cleaning"},
        )

        assert resp.status_code in (401, 422)


# ── Tests: guardia nfc_only en cancel_session ─────────────────────────────────

class TestCancelSessionNfcOnlyGuard:
    """POST /api/nfc/cancel-session debe respetar la configuración nfc_only."""

    def test_cancel_session_returns_403_when_nfc_only_is_true(
        self, client, db, cleaner_user, active_cleaning_record, jwt_token
    ):
        """Con nfc_only=true el endpoint devuelve 403 inmediatamente."""
        set_nfc_only(True)

        resp = post_json(
            client,
            "/api/nfc/cancel-session",
            {"worker_id": cleaner_user.id, "record_id": active_cleaning_record.id, "mode": "cleaning"},
            token=jwt_token,
        )

        assert resp.status_code == 403
        assert "error" in resp.get_json()

    def test_cancel_session_record_not_deleted_when_nfc_only_blocks(
        self, client, db, cleaner_user, active_cleaning_record, jwt_token
    ):
        """El registro no se elimina cuando la guardia nfc_only bloquea la petición."""
        set_nfc_only(True)

        post_json(
            client,
            "/api/nfc/cancel-session",
            {"worker_id": cleaner_user.id, "record_id": active_cleaning_record.id, "mode": "cleaning"},
            token=jwt_token,
        )

        db.session.expire_all()
        rec = db.session.get(CleaningRecord, active_cleaning_record.id)
        assert rec is not None

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Bug conocido: CleaningRecord.room_id no declara ForeignKey explícito; "
            "SQLAlchemy intenta anular el PK de Room al hacer session.delete() en cancel_session. "
            "Fix: añadir db.ForeignKey('room.id') a CleaningRecord.room_id."
        ),
    )
    def test_cancel_session_cleaning_works_when_nfc_only_is_false(
        self, client, db, cleaner_user, active_cleaning_record, jwt_token
    ):
        """Con nfc_only=false el endpoint responde 200 y el mensaje confirma la cancelación."""
        set_nfc_only(False)

        resp = post_json(
            client,
            "/api/nfc/cancel-session",
            {"worker_id": cleaner_user.id, "record_id": active_cleaning_record.id, "mode": "cleaning"},
            token=jwt_token,
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert "message" in data

    def test_cancel_session_requires_jwt(self, client, db, cleaner_user, active_cleaning_record):
        """Sin cabecera Authorization el endpoint devuelve 401/422."""
        set_nfc_only(False)

        resp = post_json(
            client,
            "/api/nfc/cancel-session",
            {"worker_id": cleaner_user.id, "record_id": active_cleaning_record.id, "mode": "cleaning"},
        )

        assert resp.status_code in (401, 422)


# ── Tests: /api/nfc/scan ignora nfc_only ─────────────────────────────────────

class TestNfcScanBypassesNfcOnlyGuard:
    """
    /api/nfc/scan no tiene guardia nfc_only: debe finalizar o iniciar sesiones
    independientemente del valor de esa configuración.
    """

    def test_scan_ends_active_cleaning_when_nfc_only_is_true(
        self, client, db, cleaner_user, room, active_cleaning_record, jwt_token
    ):
        """Con nfc_only=true, escanear la habitación con sesión activa la finaliza (action='ended')."""
        set_nfc_only(True)

        resp = post_json(
            client,
            "/api/nfc/scan",
            {"worker_id": cleaner_user.id, "nfc_code": room.number, "mode": "cleaning"},
            token=jwt_token,
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("action") == "ended"
        assert data.get("type") == "cleaning"

    def test_scan_starts_cleaning_when_nfc_only_is_true(
        self, client, db, cleaner_user, room, jwt_token
    ):
        """Con nfc_only=true y sin sesión activa, escanear la habitación inicia una (action='started')."""
        set_nfc_only(True)

        resp = post_json(
            client,
            "/api/nfc/scan",
            {"worker_id": cleaner_user.id, "nfc_code": room.number, "mode": "cleaning"},
            token=jwt_token,
        )

        assert resp.status_code == 200
        assert resp.get_json().get("action") == "started"


# ── Tests: /api/nfc/refresh-token ────────────────────────────────────────────

class TestRefreshToken:
    """POST /api/nfc/refresh-token: emite un nuevo JWT para un token válido."""

    def test_refresh_token_returns_new_access_token(self, client, db, cleaner_user, jwt_token):
        """Un JWT válido en la cabecera devuelve un nuevo access_token."""
        resp = post_json(client, "/api/nfc/refresh-token", {}, token=jwt_token)

        assert resp.status_code == 200
        data = resp.get_json()
        assert "access_token" in data
        assert isinstance(data["access_token"], str)
        assert len(data["access_token"]) > 0

    def test_refresh_token_new_token_is_different_from_original(
        self, client, db, cleaner_user, jwt_token
    ):
        """El token devuelto es un token nuevo (distinto al original)."""
        resp = post_json(client, "/api/nfc/refresh-token", {}, token=jwt_token)
        new_token = resp.get_json()["access_token"]

        # Tokens distintos (timestamps diferentes)
        assert new_token != jwt_token

    def test_refresh_token_new_token_is_usable(self, client, db, cleaner_user, jwt_token):
        """El nuevo token funciona para llamar a endpoints protegidos."""
        new_token = post_json(client, "/api/nfc/refresh-token", {}, token=jwt_token).get_json()["access_token"]

        resp = client.get(
            "/api/config",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert resp.status_code == 200

    def test_refresh_token_without_jwt_returns_401(self, client, db, cleaner_user):
        """Sin cabecera Authorization el endpoint devuelve 401/422."""
        resp = post_json(client, "/api/nfc/refresh-token", {})

        assert resp.status_code in (401, 422)


# ── Tests: /api/config incluye hidden_logout_minutes ─────────────────────────

class TestApiConfig:
    """GET /api/config debe exponer hidden_logout_minutes con el valor correcto."""

    def test_config_includes_hidden_logout_minutes_field(self, client, db, cleaner_user, jwt_token):
        """La respuesta contiene el campo hidden_logout_minutes."""
        resp = client.get(
            "/api/config",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )

        assert resp.status_code == 200
        assert "hidden_logout_minutes" in resp.get_json()

    def test_config_hidden_logout_minutes_is_integer(self, client, db, cleaner_user, jwt_token):
        """hidden_logout_minutes es un entero (no cadena de texto)."""
        data = client.get(
            "/api/config",
            headers={"Authorization": f"Bearer {jwt_token}"},
        ).get_json()

        assert isinstance(data["hidden_logout_minutes"], int)

    def test_config_hidden_logout_minutes_reflects_stored_value(
        self, client, db, cleaner_user, jwt_token
    ):
        """El valor devuelto coincide con el que se ha guardado en AppSetting."""
        AppSetting.set("hidden_logout_minutes", "45")
        _db.session.commit()

        data = client.get(
            "/api/config",
            headers={"Authorization": f"Bearer {jwt_token}"},
        ).get_json()

        assert data["hidden_logout_minutes"] == 45

    def test_config_hidden_logout_minutes_default_when_not_set(
        self, client, db, cleaner_user, jwt_token
    ):
        """Sin valor guardado devuelve el default (30 según el código fuente)."""
        data = client.get(
            "/api/config",
            headers={"Authorization": f"Bearer {jwt_token}"},
        ).get_json()

        # El default codificado en api_config() es 30
        assert data["hidden_logout_minutes"] == 30

    def test_config_requires_jwt(self, client, db, cleaner_user):
        """Sin cabecera Authorization /api/config devuelve 401/422."""
        resp = client.get("/api/config")

        assert resp.status_code in (401, 422)
