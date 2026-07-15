"""
test_rooms.py — Tests del CRUD de habitaciones, tipos de espacio y plantas.

Endpoints cubiertos:
- GET  /zonas-limpieza            → listado de habitaciones
- POST /rooms/add_edit            → alta y edición de habitaciones
- POST /rooms/delete/<id>         → baja de habitaciones
- GET  /manage_room_types         → listado de tipos de espacio
- POST /room_types/add_edit       → alta y edición de tipos de espacio
- POST /room_types/delete/<id>    → baja de tipos de espacio
- GET  /manage_floors             → listado de plantas
- POST /floors/add_edit           → alta y edición de plantas
- POST /floors/delete/<id>        → baja de plantas
"""

import pytest
from app.models import Room, RoomType, Floor
from app import db as _db


# ── HABITACIONES ──────────────────────────────────────────────────────────────

class TestManageCleaningZonesPage:
    """Tests de la página de listado de habitaciones."""

    def test_zones_page_renders_for_admin(self, auth_client, room):
        """La página devuelve 200 y muestra las habitaciones."""
        response = auth_client.get("/zonas-limpieza")
        assert response.status_code == 200
        assert room.number.encode() in response.data

    def test_zones_page_requires_auth(self, client, db):
        """Sin sesión redirige a /admin/login."""
        response = client.get("/zonas-limpieza", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


class TestAddEditRoom:
    """Tests de alta y edición de habitaciones."""

    def test_add_room_creates_record_in_db(self, auth_client, floor, room_type, app):
        """Formulario válido crea la habitación en BD."""
        auth_client.post(
            "/rooms/add_edit",
            data={
                "number": "201",
                "room_type_id": room_type.id,
                "floor_id": floor.id,
                "description": "Habitación nueva",
            },
        )
        with app.app_context():
            r = Room.query.filter_by(number="201").first()
            assert r is not None
            assert r.description == "Habitación nueva"

    def test_add_room_missing_number_shows_error(
        self, auth_client, floor, room_type
    ):
        """Sin número de habitación muestra error y no crea el registro."""
        response = auth_client.post(
            "/rooms/add_edit",
            data={
                "number": "",
                "room_type_id": room_type.id,
                "floor_id": floor.id,
                "description": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "requerido" in response.data.decode("utf-8")

    def test_add_room_missing_room_type_shows_error(
        self, auth_client, floor, room_type
    ):
        """Sin tipo de espacio muestra error."""
        response = auth_client.post(
            "/rooms/add_edit",
            data={
                "number": "301",
                "room_type_id": "",
                "floor_id": floor.id,
                "description": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "requerido" in response.data.decode("utf-8")

    def test_add_room_missing_floor_shows_error(
        self, auth_client, floor, room_type
    ):
        """Sin planta muestra error."""
        response = auth_client.post(
            "/rooms/add_edit",
            data={
                "number": "401",
                "room_type_id": room_type.id,
                "floor_id": "",
                "description": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "requerida" in response.data.decode("utf-8")

    def test_edit_room_updates_fields(self, auth_client, room, floor, room_type, app):
        """Editar con room_id actualiza los campos de la habitación."""
        auth_client.post(
            "/rooms/add_edit",
            data={
                "room_id": room.id,
                "number": "101-B",
                "room_type_id": room_type.id,
                "floor_id": floor.id,
                "description": "Descripción actualizada",
            },
        )
        with app.app_context():
            updated = Room.query.get(room.id)
            assert updated.number == "101-B"
            assert updated.description == "Descripción actualizada"

    def test_edit_nonexistent_room_does_not_crash(
        self, auth_client, floor, room_type
    ):
        """Editar un room_id que no existe no causa 500."""
        response = auth_client.post(
            "/rooms/add_edit",
            data={
                "room_id": "99999",
                "number": "X",
                "room_type_id": room_type.id,
                "floor_id": floor.id,
                "description": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_add_room_requires_auth(self, client, floor, room_type, db):
        """Sin sesión redirige a login."""
        response = client.post(
            "/rooms/add_edit",
            data={"number": "X", "room_type_id": room_type.id, "floor_id": floor.id},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


class TestDeleteRoom:
    """Tests de baja de habitaciones."""

    def test_delete_room_removes_record(self, auth_client, second_room, app):
        """Eliminar una habitación sin registros la borra de BD."""
        room_id = second_room.id
        response = auth_client.post(
            f"/rooms/delete/{room_id}",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert Room.query.get(room_id) is None

    def test_delete_nonexistent_room_returns_404(self, auth_client, db):
        """Eliminar un ID inexistente devuelve 404."""
        response = auth_client.post("/rooms/delete/99999")
        assert response.status_code == 404

    def test_delete_room_requires_auth(self, client, room):
        """Sin sesión redirige a login sin borrar nada."""
        response = client.post(
            f"/rooms/delete/{room.id}", follow_redirects=False
        )
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


# ── TIPOS DE ESPACIO ──────────────────────────────────────────────────────────

class TestManageRoomTypes:
    """Tests del CRUD de tipos de espacio."""

    def test_room_types_page_renders(self, auth_client, room_type):
        """La página de tipos de espacio muestra los tipos existentes."""
        response = auth_client.get("/manage_room_types")
        assert response.status_code == 200
        assert room_type.name.encode() in response.data

    def test_add_room_type_creates_record(self, auth_client, db, app):
        """Alta de tipo de espacio lo crea en BD."""
        auth_client.post(
            "/room_types/add_edit",
            data={"name": "Suite"},
        )
        with app.app_context():
            rt = RoomType.query.filter_by(name="Suite").first()
            assert rt is not None

    def test_edit_room_type_updates_name(self, auth_client, room_type, app):
        """Edición de tipo de espacio actualiza el nombre."""
        auth_client.post(
            "/room_types/add_edit",
            data={"room_type_id": room_type.id, "name": "Doble Actualizada"},
        )
        with app.app_context():
            rt = RoomType.query.get(room_type.id)
            assert rt.name == "Doble Actualizada"

    def test_delete_room_type_without_rooms_succeeds(self, auth_client, db, app):
        """Eliminar un tipo sin habitaciones asociadas lo borra."""
        rt = RoomType(name="Temporal")
        _db.session.add(rt)
        _db.session.commit()
        rt_id = rt.id

        response = auth_client.post(
            f"/room_types/delete/{rt_id}", follow_redirects=False
        )
        assert response.status_code == 302
        with app.app_context():
            assert RoomType.query.get(rt_id) is None

    def test_delete_room_type_in_use_is_rejected(
        self, auth_client, room_type, room
    ):
        """Eliminar un tipo en uso por habitaciones muestra error."""
        response = auth_client.post(
            f"/room_types/delete/{room_type.id}",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "No se puede eliminar" in response.data.decode("utf-8")

    def test_room_types_requires_auth(self, client, db):
        """Sin sesión redirige a login."""
        response = client.get("/manage_room_types", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


# ── PLANTAS ───────────────────────────────────────────────────────────────────

class TestManageFloors:
    """Tests del CRUD de plantas."""

    def test_floors_page_renders(self, auth_client, floor):
        """La página de plantas muestra las plantas existentes."""
        response = auth_client.get("/manage_floors")
        assert response.status_code == 200
        assert floor.name.encode() in response.data

    def test_add_floor_creates_record(self, auth_client, db, app):
        """Alta de planta la crea en BD."""
        auth_client.post(
            "/floors/add_edit",
            data={"name": "Primera Planta"},
        )
        with app.app_context():
            f = Floor.query.filter_by(name="Primera Planta").first()
            assert f is not None

    def test_edit_floor_updates_name(self, auth_client, floor, app):
        """Edición de planta actualiza el nombre."""
        auth_client.post(
            "/floors/add_edit",
            data={"floor_id": floor.id, "name": "Planta Alta"},
        )
        with app.app_context():
            f = Floor.query.get(floor.id)
            assert f.name == "Planta Alta"

    def test_delete_floor_without_rooms_succeeds(self, auth_client, db, app):
        """Eliminar una planta sin habitaciones la borra."""
        f = Floor(name="Planta Temporal")
        _db.session.add(f)
        _db.session.commit()
        floor_id = f.id

        response = auth_client.post(
            f"/floors/delete/{floor_id}", follow_redirects=False
        )
        assert response.status_code == 302
        with app.app_context():
            assert Floor.query.get(floor_id) is None

    def test_delete_floor_in_use_is_rejected(self, auth_client, floor, room):
        """Eliminar una planta que tiene habitaciones muestra error."""
        response = auth_client.post(
            f"/floors/delete/{floor.id}",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "No se puede eliminar" in response.data.decode("utf-8")

    def test_floors_requires_auth(self, client, db):
        """Sin sesión redirige a login."""
        response = client.get("/manage_floors", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]
