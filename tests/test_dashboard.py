"""
test_dashboard.py — Tests de la pantalla de inicio del panel admin.

Cubre el refresco automatico cada 15 segundos y el limite de generacion de
notificaciones que evita repetir el escaneo completo en cada recarga.
"""

from datetime import datetime, timedelta

import pytest

from app.blueprints import admin as admin_bp_module


@pytest.fixture(scope="function", autouse=True)
def reset_notif_throttle():
    """Cada test parte sin marca de la ultima generacion de notificaciones."""
    admin_bp_module._last_notif_generation = None
    yield
    admin_bp_module._last_notif_generation = None


class TestDashboardAutoRefresh:
    """Refresco automatico de la portada."""

    def test_la_portada_incluye_el_interruptor_de_refresco(self, auth_client):
        """La pantalla de inicio trae el switch y el temporizador de 15 s."""
        response = auth_client.get("/")
        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="auto-refresh-toggle"' in html
        assert "Actualización automática" in html
        assert "var SEGUNDOS = 15;" in html

    def test_otras_pantallas_no_se_refrescan(self, auth_client, room):
        """El refresco es solo de la portada, no del resto del panel."""
        response = auth_client.get("/zonas-limpieza")
        assert response.status_code == 200
        assert 'id="auto-refresh-toggle"' not in response.data.decode()

    def test_la_portada_requiere_admin(self, client, db):
        """Sin sesion redirige a /admin/login."""
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


class TestNotificationThrottle:
    """Limite de generacion de notificaciones."""

    def test_primera_visita_genera_notificaciones(self, auth_client, monkeypatch):
        """Sin marca previa, el escaneo se ejecuta."""
        llamadas = []
        monkeypatch.setattr(
            "app.blueprints.notifications._generate_notifications",
            lambda: llamadas.append(1),
        )
        # El login del fixture ya ha pasado por la portada: se reinicia la marca
        admin_bp_module._last_notif_generation = None
        auth_client.get("/")
        assert len(llamadas) == 1

    def test_recargas_seguidas_no_repiten_el_escaneo(self, auth_client, monkeypatch):
        """Las recargas de los 15 s no vuelven a generar notificaciones."""
        llamadas = []
        monkeypatch.setattr(
            "app.blueprints.notifications._generate_notifications",
            lambda: llamadas.append(1),
        )
        # El login del fixture ya ha pasado por la portada: se reinicia la marca
        admin_bp_module._last_notif_generation = None
        auth_client.get("/")
        auth_client.get("/")
        auth_client.get("/")
        assert len(llamadas) == 1

    def test_pasado_el_intervalo_vuelve_a_generar(self, auth_client, monkeypatch):
        """Superados los 2 minutos, el escaneo se repite."""
        llamadas = []
        monkeypatch.setattr(
            "app.blueprints.notifications._generate_notifications",
            lambda: llamadas.append(1),
        )
        # El login del fixture ya ha pasado por la portada: se reinicia la marca
        admin_bp_module._last_notif_generation = None
        auth_client.get("/")
        admin_bp_module._last_notif_generation = datetime.now() - timedelta(
            seconds=admin_bp_module.NOTIF_REFRESH_SECONDS + 1
        )
        auth_client.get("/")
        assert len(llamadas) == 2
