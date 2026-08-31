"""
test_compliance_audit.py — Auditoría de cumplimiento y KPIs de calidad.

Endpoints cubiertos:
- GET /admin/compliance-audit
- GET /admin/quality-kpis

El barrido de `test_smoke_routes.py` recorre estas dos rutas, pero con la base de
datos vacía: los bucles que consultan formación y residentes no llegan a
ejecutarse. `/admin/compliance-audit` devolvía 500 en cuanto había una píldora
activa, y nadie se enteraba. Estos tests las cargan **con datos**.
"""

import pytest

from app.models import Cleaner, Resident, TrainingPill, TrainingCompletion


@pytest.fixture
def datos_de_cumplimiento(db, admin_user):
    """Lo mínimo para que los bucles de ambas páginas entren: píldora + residente."""
    pill = TrainingPill(title='Higiene de manos', pass_threshold=80,
                        active=True, assign_mode='all', created_by=admin_user.id)
    resident = Resident(name='Josefa Ruiz', nfc_code='RES-TEST-1', active=True)
    worker = Cleaner(username='fatima', name='Fatima Zahra', active=True)
    worker.set_password('pass1234')
    db.session.add_all([pill, resident, worker])
    db.session.flush()
    db.session.add(TrainingCompletion(pill_id=pill.id, cleaner_id=worker.id,
                                      score=90, passed=True))
    db.session.commit()
    return pill


def test_auditoria_de_cumplimiento_carga_con_formacion_activa(
        auth_client, db, datos_de_cumplimiento):
    """Consultaba una columna inexistente y devolvía 500 en cuanto había una píldora."""
    response = auth_client.get('/admin/compliance-audit')

    assert response.status_code == 200


def test_kpis_de_calidad_cargan_con_datos(auth_client, db, datos_de_cumplimiento):
    """No estaba rota, pero es una página grande sin ninguna cobertura previa."""
    response = auth_client.get('/admin/quality-kpis')

    assert response.status_code == 200


def test_la_auditoria_detecta_quien_tiene_formacion_pendiente(
        auth_client, db, datos_de_cumplimiento):
    """Hay dos personas activas y solo una aprobó la píldora: queda una pendiente."""
    html = auth_client.get('/admin/compliance-audit').get_data(as_text=True)

    assert '1 trabajadores pendientes' in html


def test_ambas_rutas_requieren_admin(client, db, datos_de_cumplimiento):
    for ruta in ('/admin/compliance-audit', '/admin/quality-kpis'):
        assert client.get(ruta).status_code in (302, 401, 403)
