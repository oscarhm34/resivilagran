"""
test_finalizar_atencion.py — Cerrar una atención siempre debe ser posible.

Si no hay tipos de atención configurados, la pantalla de finalizar no ofrece
ninguno que seleccionar. Exigir una selección dejaba a la trabajadora atrapada
con la sesión abierta, que acababa cerrándose sola a las 24 horas con una
duración falsa.

El backend siempre aceptó la lista vacía; era la webapp la que bloqueaba. Estos
tests fijan el contrato del servidor para que no se endurezca por accidente.
"""

from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import Cleaner, Resident, CareType, CareRecord


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def atencion_abierta(db, cleaner_user):
    resident = Resident(name='Josefa Ruiz', nfc_code='RES-FIN-1', active=True)
    db.session.add(resident)
    db.session.flush()
    r = CareRecord(worker_id=cleaner_user.id, resident_id=resident.id,
                   start_time=datetime.now() - timedelta(minutes=15))
    db.session.add(r)
    db.session.commit()
    return r


def test_se_puede_cerrar_sin_ningun_tipo_de_atencion(
        client, db, atencion_abierta, cleaner_user, worker_headers):
    """Es el caso de una residencia sin tipos configurados todavía."""
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [],
    })

    assert res.status_code == 200
    assert db.session.get(CareRecord, atencion_abierta.id).end_time is not None


def test_cerrar_con_tipo_lo_deja_registrado(
        client, db, atencion_abierta, cleaner_user, worker_headers):
    ct = CareType(name='Higiene')
    db.session.add(ct)
    db.session.commit()

    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [ct.id],
        'notes': 'Sin incidencias',
    })

    assert res.status_code == 200
    registro = db.session.get(CareRecord, atencion_abierta.id)
    assert [c.name for c in registro.care_types] == ['Higiene']
    assert registro.notes == 'Sin incidencias'


def test_no_se_puede_cerrar_la_atencion_de_otra_persona(
        client, db, atencion_abierta, admin_user, worker_headers):
    """`_verify_worker_id` tiene que cortar el intento."""
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': admin_user.id,
        'care_type_ids': [],
    })

    assert res.status_code == 403
    assert db.session.get(CareRecord, atencion_abierta.id).end_time is None


def test_no_se_puede_cerrar_dos_veces(
        client, db, atencion_abierta, cleaner_user, worker_headers):
    cuerpo = {'record_id': atencion_abierta.id, 'worker_id': cleaner_user.id,
              'care_type_ids': []}
    client.post('/api/nfc/finalize-care', headers=worker_headers, json=cuerpo)

    segunda = client.post('/api/nfc/finalize-care', headers=worker_headers, json=cuerpo)

    assert segunda.status_code == 400
