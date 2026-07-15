"""
test_auth.py — Tests de autenticación web (panel de administración).

Cubre:
- Login correcto con credenciales de admin
- Login con contraseña incorrecta
- Login con usuario inexistente
- Login con usuario sin permisos de admin (limpiadora)
- Acceso a rutas protegidas sin sesión → redirección a /admin/login
- Redirección al index cuando ya hay sesión activa
- Logout correcto
"""

import pytest


class TestAdminLogin:
    """Tests del endpoint GET/POST /admin/login."""

    def test_login_page_renders(self, client, db):
        """La página de login debe responder con 200."""
        response = client.get("/admin/login")
        assert response.status_code == 200

    def test_login_correct_credentials_redirects_to_index(self, client, admin_user):
        """Login con credenciales válidas de admin redirige al índice."""
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        # Tras login correcto llegamos al índice, no a la página de login
        assert b"admin/login" not in response.data

    def test_login_wrong_password_shows_error(self, client, admin_user):
        """Login con contraseña incorrecta devuelve 200 con mensaje de error."""
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "wrongpassword"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Credenciales incorrectas" in response.data.decode("utf-8")

    def test_login_nonexistent_user_shows_error(self, client, db):
        """Login con usuario que no existe muestra error."""
        response = client.post(
            "/admin/login",
            data={"username": "noexiste", "password": "cualquiera"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Credenciales incorrectas" in response.data.decode("utf-8")

    def test_login_non_admin_user_is_rejected(self, client, cleaner_user):
        """Un usuario sin is_admin=True no puede acceder al panel web."""
        response = client.post(
            "/admin/login",
            data={"username": "limpiadora1", "password": "limpia123"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Credenciales incorrectas" in response.data.decode("utf-8")

    def test_already_authenticated_user_is_redirected_to_index(self, auth_client):
        """Un admin ya logado que visita /admin/login es redirigido al índice."""
        response = auth_client.get("/admin/login", follow_redirects=True)
        assert response.status_code == 200
        # No debe volver a mostrar el formulario de login
        assert b"admin/login" not in response.data


class TestProtectedRoutes:
    """Tests de que las rutas @login_required redirigen a /admin/login."""

    PROTECTED_ROUTES = [
        "/",
        "/manage_workers",
        "/zonas-limpieza",
        "/manage_room_types",
        "/manage_floors",
        "/registros-limpieza",
        "/exportar_excel",
        "/ultima-limpieza",
    ]

    @pytest.mark.parametrize("route", PROTECTED_ROUTES)
    def test_unauthenticated_access_redirects_to_login(self, client, db, route):
        """
        Cualquier ruta protegida sin sesión debe redirigir a /admin/login,
        no devolver 200 ni 403.
        """
        response = client.get(route, follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]

    @pytest.mark.parametrize("route", PROTECTED_ROUTES)
    def test_authenticated_admin_can_access_protected_routes(
        self, auth_client, room, floor, room_type, route
    ):
        """Un admin autenticado obtiene 200 en todas las rutas protegidas."""
        response = auth_client.get(route)
        assert response.status_code == 200


class TestLogout:
    """Tests del endpoint POST /admin/logout."""

    def test_logout_ends_session(self, auth_client):
        """Después de logout, el acceso a rutas protegidas redirige a login."""
        # Verificar que estamos autenticados
        response = auth_client.get("/")
        assert response.status_code == 200

        # Cerrar sesión
        auth_client.post("/admin/logout", follow_redirects=True)

        # Ahora el acceso debe redirigir al login
        response = auth_client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]

    def test_logout_redirects_to_login_page(self, auth_client):
        """El logout redirige a la página de login."""
        response = auth_client.post("/admin/logout", follow_redirects=True)
        assert response.status_code == 200

    def test_logout_unauthenticated_redirects(self, client, db):
        """Un POST a /admin/logout sin sesión redirige (no da 500)."""
        response = client.post("/admin/logout", follow_redirects=False)
        assert response.status_code == 302
