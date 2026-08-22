"""
test_resident_documents.py — Tests de documentos del residente.

Cubre la subida y el borrado de documentos desde la ficha del residente
(pestaña Documentos) y desde el listado de residentes.

Endpoints cubiertos:
- POST /residents/<id>/documents          → subir documento
- POST /residents/documents/<id>/delete   → eliminar documento
"""

import io
import os

import pytest

from app.models import Resident, ResidentDocument


@pytest.fixture(scope="function")
def upload_folder(app, tmp_path):
    """Carpeta de subidas temporal: los tests no escriben en uploads/ real."""
    original = app.config.get("UPLOAD_FOLDER")
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    yield str(tmp_path)
    app.config["UPLOAD_FOLDER"] = original


@pytest.fixture(scope="function")
def residente(db):
    """Residente de prueba."""
    r = Resident(name="Ana Ejemplo", nfc_code="NFC-DOC", room_number="101", active=True)
    db.session.add(r)
    db.session.commit()
    return r


def _fichero(nombre="pia.pdf", contenido=b"%PDF-1.4 test"):
    return (io.BytesIO(contenido), nombre)


class TestUploadResidentDocument:
    """Subida de documentos."""

    def test_subida_crea_documento_y_fichero(
        self, auth_client, db, residente, upload_folder, app
    ):
        """El documento queda en BD y el fichero en la carpeta de subidas."""
        response = auth_client.post(
            f"/residents/{residente.id}/documents",
            data={
                "doc_file": _fichero(),
                "doc_type": "PIA",
                "doc_description": "PIA 2026",
                "next": f"/admin/resident/{residente.id}#tab-documentos",
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        assert response.headers["Location"] == f"/admin/resident/{residente.id}#tab-documentos"

        with app.app_context():
            doc = ResidentDocument.query.filter_by(resident_id=residente.id).first()
            assert doc is not None
            assert doc.original_filename == "pia.pdf"
            assert doc.doc_type == "PIA"
            assert doc.description == "PIA 2026"
            assert os.path.exists(os.path.join(upload_folder, doc.file_path))

    def test_extension_no_permitida_no_crea_documento(
        self, auth_client, db, residente, upload_folder, app
    ):
        """Un .exe se rechaza y no llega a BD."""
        response = auth_client.post(
            f"/residents/{residente.id}/documents",
            data={"doc_file": _fichero("virus.exe", b"MZ")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        with app.app_context():
            assert ResidentDocument.query.count() == 0

    def test_sin_fichero_no_crea_documento(
        self, auth_client, db, residente, upload_folder, app
    ):
        """Enviar el formulario vacío no crea nada."""
        auth_client.post(
            f"/residents/{residente.id}/documents",
            data={"doc_type": "Otros"},
            content_type="multipart/form-data",
        )
        with app.app_context():
            assert ResidentDocument.query.count() == 0

    def test_next_externo_se_ignora(
        self, auth_client, db, residente, upload_folder
    ):
        """Un destino fuera de la aplicación no se usa para redirigir."""
        response = auth_client.post(
            f"/residents/{residente.id}/documents",
            data={"doc_file": _fichero(), "next": "http://evil.example/x"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        assert "evil.example" not in response.headers["Location"]
        assert "manage-residents" in response.headers["Location"]

    def test_requiere_admin(self, client, db, residente, upload_folder):
        """Sin sesión redirige a /admin/login y no sube nada."""
        response = client.post(
            f"/residents/{residente.id}/documents",
            data={"doc_file": _fichero()},
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]
        assert ResidentDocument.query.count() == 0


class TestDeleteResidentDocument:
    """Borrado de documentos."""

    def test_borrado_elimina_registro_y_vuelve_a_la_ficha(
        self, auth_client, db, residente, upload_folder, app
    ):
        """El documento desaparece de BD y se vuelve a la pestaña Documentos."""
        auth_client.post(
            f"/residents/{residente.id}/documents",
            data={"doc_file": _fichero()},
            content_type="multipart/form-data",
        )
        doc = ResidentDocument.query.filter_by(resident_id=residente.id).first()
        ruta = os.path.join(upload_folder, doc.file_path)
        assert os.path.exists(ruta)

        response = auth_client.post(
            f"/residents/documents/{doc.id}/delete",
            data={"next": f"/admin/resident/{residente.id}#tab-documentos"},
        )
        assert response.status_code == 302
        assert response.headers["Location"] == f"/admin/resident/{residente.id}#tab-documentos"

        with app.app_context():
            assert db.session.get(ResidentDocument, doc.id) is None
            assert not os.path.exists(ruta)

    def test_requiere_admin(self, client, db, residente, upload_folder, app):
        """Sin sesión no se borra nada."""
        doc = ResidentDocument(
            resident_id=residente.id,
            file_path="resident_docs/x.pdf",
            original_filename="x.pdf",
        )
        db.session.add(doc)
        db.session.commit()

        response = client.post(f"/residents/documents/{doc.id}/delete")
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]
        with app.app_context():
            assert db.session.get(ResidentDocument, doc.id) is not None


class TestResidentDetailDocumentsTab:
    """Pestaña Documentos de la ficha."""

    def test_la_ficha_muestra_el_formulario_de_subida(
        self, auth_client, residente
    ):
        """La ficha incluye el formulario para subir documentos."""
        response = auth_client.get(f"/admin/resident/{residente.id}")
        assert response.status_code == 200
        html = response.data.decode()
        assert f'action="/residents/{residente.id}/documents"' in html
        assert 'name="doc_file"' in html
