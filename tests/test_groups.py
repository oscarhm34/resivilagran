"""
test_groups.py — Tests de grupos de residentes.

Regla cubierta: un residente inactivo (dado de baja) no forma parte de ningún
grupo — no aparece en el detalle del grupo, no cuenta en el listado y no puede
asignarse a uno.

Endpoints cubiertos:
- GET  /manage-groups                    → listado de grupos y su recuento
- GET  /groups/<id>                      → detalle del grupo
- POST /groups/<id>/assign-residents     → asignación masiva desde el grupo
- POST /residents/update-group           → cambio de grupo desde el listado
"""

import pytest

from app.models import Resident, ResidentGroup


@pytest.fixture(scope="function")
def grupo(db):
    """Grupo de residentes de prueba."""
    g = ResidentGroup(name="Planta 1", color="#0069d9")
    db.session.add(g)
    db.session.commit()
    return g


@pytest.fixture(scope="function")
def residente_activo(db, grupo):
    """Residente de alta asignado al grupo."""
    r = Resident(name="Ana Activa", nfc_code="NFC-ACT", room_number="101",
                 active=True, group_id=grupo.id)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture(scope="function")
def residente_inactivo(db, grupo):
    """Residente de baja que conserva el grupo asignado."""
    r = Resident(name="Berta Baja", nfc_code="NFC-BAJA", room_number="102",
                 active=False, group_id=grupo.id)
    db.session.add(r)
    db.session.commit()
    return r


class TestGroupDetail:
    """Detalle de un grupo."""

    def test_detalle_no_muestra_residentes_inactivos(
        self, auth_client, grupo, residente_activo, residente_inactivo
    ):
        """Solo los residentes de alta aparecen en el grupo."""
        response = auth_client.get(f"/groups/{grupo.id}")
        assert response.status_code == 200
        assert "Ana Activa".encode() in response.data
        assert "Berta Baja".encode() not in response.data

    def test_detalle_requiere_admin(self, client, grupo):
        """Sin sesión redirige a /admin/login."""
        response = client.get(f"/groups/{grupo.id}", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


class TestManageGroups:
    """Listado de grupos."""

    def test_recuento_ignora_residentes_inactivos(
        self, auth_client, grupo, residente_activo, residente_inactivo
    ):
        """El grupo con 1 activo y 1 de baja cuenta 1 residente."""
        response = auth_client.get("/manage-groups")
        assert response.status_code == 200
        fila = response.data.decode().split("Planta 1")[1]
        assert "<td>1</td>" in fila


class TestAssignResidentsToGroup:
    """Asignación masiva desde la ficha del grupo."""

    def test_no_asigna_residentes_inactivos(
        self, auth_client, db, grupo, residente_inactivo, app
    ):
        """Un residente de baja se omite y su grupo no cambia."""
        residente_inactivo.group_id = None
        db.session.commit()

        response = auth_client.post(
            f"/groups/{grupo.id}/assign-residents",
            json={"resident_ids": [residente_inactivo.id]},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["count"] == 0
        assert data["skipped"] == 1

        with app.app_context():
            r = db.session.get(Resident, residente_inactivo.id)
            assert r.group_id is None

    def test_asigna_residentes_activos(self, auth_client, db, grupo, app):
        """Un residente de alta sí se asigna al grupo."""
        r = Resident(name="Clara Alta", nfc_code="NFC-ALTA", active=True)
        db.session.add(r)
        db.session.commit()

        response = auth_client.post(
            f"/groups/{grupo.id}/assign-residents",
            json={"resident_ids": [r.id]},
        )
        assert response.status_code == 200
        assert response.get_json()["count"] == 1

        with app.app_context():
            assert db.session.get(Resident, r.id).group_id == grupo.id


class TestUpdateResidentGroup:
    """Cambio de grupo desde el listado de residentes."""

    def test_residente_inactivo_no_puede_entrar_en_un_grupo(
        self, auth_client, db, grupo, residente_inactivo, app
    ):
        """Devuelve 400 y no modifica el grupo."""
        residente_inactivo.group_id = None
        db.session.commit()

        response = auth_client.post(
            "/residents/update-group",
            json={"resident_id": residente_inactivo.id, "group_id": grupo.id},
        )
        assert response.status_code == 400
        assert "baja" in response.get_json()["error"]

        with app.app_context():
            assert db.session.get(Resident, residente_inactivo.id).group_id is None

    def test_residente_inactivo_puede_salir_del_grupo(
        self, auth_client, db, grupo, residente_inactivo, app
    ):
        """Quitar el grupo (group_id nulo) sigue permitido."""
        response = auth_client.post(
            "/residents/update-group",
            json={"resident_id": residente_inactivo.id, "group_id": None},
        )
        assert response.status_code == 200

        with app.app_context():
            assert db.session.get(Resident, residente_inactivo.id).group_id is None
