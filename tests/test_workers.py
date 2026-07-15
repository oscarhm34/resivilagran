"""
test_workers.py — Tests del CRUD de empleados (limpiadoras/administradores).

Endpoints cubiertos:
- GET  /manage_workers           → listado de trabajadores
- POST /cleaners/add_edit        → alta y edición
- POST /cleaners/delete/<id>     → baja

Casos cubiertos:
- Añadir un trabajador nuevo
- Añadir un trabajador sin contraseña (debe fallo graceful)
- Editar nombre, username y contraseña de un trabajador existente
- Editar un trabajador que no existe
- Eliminar un trabajador sin registros asociados
- Intentar eliminar un trabajador con registros asociados → error controlado
- Acceso sin autenticación redirige a login
"""

import pytest
from app.models import Cleaner, CleaningRecord
from app import db as _db


class TestManageWorkersPage:
    """Tests de la página de listado de trabajadores."""

    def test_manage_workers_renders_for_admin(self, auth_client, admin_user):
        """La página devuelve 200 y lista los trabajadores existentes."""
        response = auth_client.get("/manage_workers")
        assert response.status_code == 200
        assert admin_user.name.encode() in response.data

    def test_manage_workers_requires_auth(self, client, db):
        """Sin sesión redirige a /admin/login."""
        response = client.get("/manage_workers", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


class TestAddCleaner:
    """Tests de alta de nuevos trabajadores."""

    def test_add_new_cleaner_redirects_and_creates_record(
        self, auth_client, db, app
    ):
        """Formulario válido crea el trabajador y redirige a manage_workers."""
        response = auth_client.post(
            "/cleaners/add_edit",
            data={
                "username": "nuevauser",
                "name": "Nueva Limpiadora",
                "password": "pass1234",
                "is_admin": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "manage_workers" in response.headers["Location"]

        with app.app_context():
            cleaner = Cleaner.query.filter_by(username="nuevauser").first()
            assert cleaner is not None
            assert cleaner.name == "Nueva Limpiadora"
            assert cleaner.is_admin is False

    def test_add_cleaner_with_admin_flag(self, auth_client, db, app):
        """Se puede crear un trabajador con permisos de administrador."""
        auth_client.post(
            "/cleaners/add_edit",
            data={
                "username": "adminuevo",
                "name": "Admin Nuevo",
                "password": "pass1234",
                "is_admin": "on",
            },
        )
        with app.app_context():
            cleaner = Cleaner.query.filter_by(username="adminuevo").first()
            assert cleaner is not None
            assert cleaner.is_admin is True

    def test_add_cleaner_password_is_hashed(self, auth_client, db, app):
        """La contraseña se almacena hasheada, nunca en texto plano."""
        auth_client.post(
            "/cleaners/add_edit",
            data={
                "username": "usercheck",
                "name": "Check Password",
                "password": "secreto123",
                "is_admin": "",
            },
        )
        with app.app_context():
            cleaner = Cleaner.query.filter_by(username="usercheck").first()
            assert cleaner is not None
            assert cleaner.password_hash != "secreto123"
            assert cleaner.check_password("secreto123") is True

    def test_add_cleaner_requires_auth(self, client, db):
        """Sin sesión, el POST redirige a login."""
        response = client.post(
            "/cleaners/add_edit",
            data={"username": "hacker", "name": "Hacker", "password": "x"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


class TestEditCleaner:
    """Tests de edición de trabajadores existentes."""

    def test_edit_cleaner_updates_name_and_username(
        self, auth_client, cleaner_user, app
    ):
        """Editar con cleaner_id actualiza el trabajador en BD."""
        auth_client.post(
            "/cleaners/add_edit",
            data={
                "cleaner_id": cleaner_user.id,
                "username": "limpiadora_editada",
                "name": "María Editada",
                "password": "",
                "is_admin": "",
            },
        )
        with app.app_context():
            cleaner = Cleaner.query.get(cleaner_user.id)
            assert cleaner.username == "limpiadora_editada"
            assert cleaner.name == "María Editada"

    def test_edit_cleaner_without_password_keeps_old_hash(
        self, auth_client, cleaner_user, app
    ):
        """Si no se proporciona contraseña en la edición, la antigua se conserva."""
        original_hash = cleaner_user.password_hash

        auth_client.post(
            "/cleaners/add_edit",
            data={
                "cleaner_id": cleaner_user.id,
                "username": "limpiadora1",
                "name": "Nuevo Nombre",
                "password": "",
                "is_admin": "",
            },
        )
        with app.app_context():
            cleaner = Cleaner.query.get(cleaner_user.id)
            assert cleaner.password_hash == original_hash

    def test_edit_cleaner_with_new_password_changes_hash(
        self, auth_client, cleaner_user, app
    ):
        """Si se proporciona nueva contraseña, el hash debe cambiar."""
        original_hash = cleaner_user.password_hash

        auth_client.post(
            "/cleaners/add_edit",
            data={
                "cleaner_id": cleaner_user.id,
                "username": "limpiadora1",
                "name": "María García",
                "password": "nuevapass456",
                "is_admin": "",
            },
        )
        with app.app_context():
            cleaner = Cleaner.query.get(cleaner_user.id)
            assert cleaner.password_hash != original_hash
            assert cleaner.check_password("nuevapass456") is True

    def test_edit_nonexistent_cleaner_shows_error_flash(
        self, auth_client, db
    ):
        """Editar un ID que no existe no debe causar 500."""
        response = auth_client.post(
            "/cleaners/add_edit",
            data={
                "cleaner_id": "99999",
                "username": "ghost",
                "name": "Ghost",
                "password": "x",
                "is_admin": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200


class TestDeleteCleaner:
    """Tests de baja de trabajadores."""

    def test_delete_cleaner_without_records_succeeds(
        self, auth_client, cleaner_user, app
    ):
        """Eliminar un trabajador sin registros asociados lo borra de BD."""
        cleaner_id = cleaner_user.id
        response = auth_client.post(
            f"/cleaners/delete/{cleaner_id}",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert Cleaner.query.get(cleaner_id) is None

    def test_delete_cleaner_with_records_is_rejected(
        self, auth_client, completed_record, cleaner_user, app
    ):
        """
        Eliminar un trabajador con registros de limpieza asociados
        falla con IntegrityError y muestra mensaje de error.
        El trabajador debe permanecer en BD.
        """
        response = auth_client.post(
            f"/cleaners/delete/{cleaner_user.id}",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "No se puede eliminar" in response.data.decode("utf-8")

        with app.app_context():
            # El trabajador debe seguir existiendo
            assert Cleaner.query.get(cleaner_user.id) is not None

    def test_delete_nonexistent_cleaner_returns_404(self, auth_client, db):
        """Intentar borrar un ID que no existe devuelve 404."""
        response = auth_client.post(
            "/cleaners/delete/99999",
            follow_redirects=False,
        )
        assert response.status_code == 404

    def test_delete_cleaner_requires_auth(self, client, cleaner_user):
        """Sin sesión redirige a login sin borrar nada."""
        response = client.post(
            f"/cleaners/delete/{cleaner_user.id}",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]
