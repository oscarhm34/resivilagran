"""
test_medicacion.py — Registro de administración de fármacos.

Endpoint cubierto: POST /api/medication/administer (webapp de trabajadoras).

Es el sitio del proyecto donde un dato mal guardado tiene peores consecuencias, y
no tenía ningún test funcional. El `status` llegaba del cliente sin validar, así
que cualquier cadena acababa en la base de datos.
"""

import pytest
from flask_jwt_extended import create_access_token

from app.models import (Cleaner, Resident, MedicationPrescription,
                        MedicationAdministration, Notification)


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def pauta(db):
    resident = Resident(name='Josefa Ruiz', nfc_code='RES-MED-1', active=True)
    db.session.add(resident)
    db.session.flush()
    p = MedicationPrescription(
        resident_id=resident.id, drug_name='Sintrom', dose='4 mg',
        frequency='diaria', schedule_times='09:00', active=True,
    )
    db.session.add(p)
    db.session.commit()
    return p


def test_administrar_guarda_el_registro(client, db, pauta, cleaner_user, worker_headers):
    res = client.post('/api/medication/administer', headers=worker_headers, json={
        'prescription_id': pauta.id, 'scheduled_time': '09:00',
        'status': 'given', 'notes': 'Sin incidencias',
    })

    assert res.status_code == 200
    registro = MedicationAdministration.query.filter_by(prescription_id=pauta.id).one()
    assert registro.status == 'given'
    assert registro.administered_by == cleaner_user.id
    assert registro.scheduled_time == '09:00'


@pytest.mark.parametrize('estado', ['refused', 'omitted', 'not_available'])
def test_los_estados_que_no_son_administrado_generan_aviso(
        client, db, pauta, worker_headers, estado):
    """Que una residente rechace la medicación tiene que llegar a alguien."""
    client.post('/api/medication/administer', headers=worker_headers, json={
        'prescription_id': pauta.id, 'scheduled_time': '09:00', 'status': estado,
    })

    assert Notification.query.filter_by(type='medication_alert').count() == 1


def test_un_estado_inventado_se_rechaza(client, db, pauta, worker_headers):
    """Sin lista blanca se guardaba cualquier cadena como estado de administración."""
    res = client.post('/api/medication/administer', headers=worker_headers, json={
        'prescription_id': pauta.id, 'scheduled_time': '09:00',
        'status': 'lo-que-sea',
    })

    assert res.status_code == 400
    assert MedicationAdministration.query.count() == 0


def test_una_pauta_inexistente_devuelve_404(client, db, worker_headers):
    res = client.post('/api/medication/administer', headers=worker_headers, json={
        'prescription_id': 99999, 'scheduled_time': '09:00', 'status': 'given',
    })

    assert res.status_code == 404
    assert MedicationAdministration.query.count() == 0


def test_administrar_requiere_jwt(client, db, pauta):
    res = client.post('/api/medication/administer', json={
        'prescription_id': pauta.id, 'status': 'given',
    })

    assert res.status_code == 401
    assert MedicationAdministration.query.count() == 0
