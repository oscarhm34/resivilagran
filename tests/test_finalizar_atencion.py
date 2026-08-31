"""
test_finalizar_atencion.py — Ninguna atención se guarda sin tipo.

Un registro cerrado sin tipo no dice qué se hizo con el residente y ya no hay
manera de reconstruirlo. El servidor lo rechaza, tanto en el cierre individual
como en el de grupo: no basta con que la webapp lo pida, porque cuando la
webapp no consiguió cargar los tipos fue precisamente el servidor el que
aceptó la lista vacía y dejó dos registros incompletos.

La sesión se queda abierta y puede finalizarse más tarde; nunca se cierra a
medias.
"""

from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import Resident, CareType, CareRecord


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


@pytest.fixture
def tipo_higiene(db):
    ct = CareType(name='Higiene')
    db.session.add(ct)
    db.session.commit()
    return ct


def test_no_se_puede_cerrar_sin_ningun_tipo_de_atencion(
        client, db, atencion_abierta, cleaner_user, worker_headers):
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [],
    })

    assert res.status_code == 400
    assert 'tipo de atención' in res.get_json()['error']
    # La sesión sigue abierta: se podrá finalizar cuando haya tipos que elegir.
    assert db.session.get(CareRecord, atencion_abierta.id).end_time is None


def test_no_se_puede_cerrar_con_tipos_inexistentes(
        client, db, atencion_abierta, cleaner_user, worker_headers):
    """Mandar ids que no existen equivale a no mandar ninguno."""
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [9999],
    })

    assert res.status_code == 400
    assert db.session.get(CareRecord, atencion_abierta.id).end_time is None


def test_cerrar_con_tipo_lo_deja_registrado(
        client, db, atencion_abierta, cleaner_user, worker_headers, tipo_higiene):
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [tipo_higiene.id],
        'notes': 'Sin incidencias',
    })

    assert res.status_code == 200
    registro = db.session.get(CareRecord, atencion_abierta.id)
    assert [c.name for c in registro.care_types] == ['Higiene']
    assert registro.notes == 'Sin incidencias'
    assert registro.end_time is not None


def test_no_se_puede_cerrar_la_atencion_de_otra_persona(
        client, db, atencion_abierta, admin_user, worker_headers, tipo_higiene):
    """`_verify_worker_id` tiene que cortar el intento."""
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': admin_user.id,
        'care_type_ids': [tipo_higiene.id],
    })

    assert res.status_code == 403
    assert db.session.get(CareRecord, atencion_abierta.id).end_time is None


def test_no_se_puede_cerrar_dos_veces(
        client, db, atencion_abierta, cleaner_user, worker_headers, tipo_higiene):
    cuerpo = {'record_id': atencion_abierta.id, 'worker_id': cleaner_user.id,
              'care_type_ids': [tipo_higiene.id]}
    client.post('/api/nfc/finalize-care', headers=worker_headers, json=cuerpo)

    segunda = client.post('/api/nfc/finalize-care', headers=worker_headers, json=cuerpo)

    assert segunda.status_code == 400


# ── Cierre de grupo ───────────────────────────────────────────────────────

@pytest.fixture
def grupo_abierto(db, cleaner_user):
    registros = []
    for i, nombre in enumerate(('Pilar Gomez', 'Antonio Diaz')):
        resident = Resident(name=nombre, nfc_code=f'RES-GRP-{i}', active=True)
        db.session.add(resident)
        db.session.flush()
        r = CareRecord(worker_id=cleaner_user.id, resident_id=resident.id,
                       start_time=datetime.now() - timedelta(minutes=10))
        db.session.add(r)
        registros.append(r)
    db.session.commit()
    return registros


def test_el_grupo_no_se_cierra_si_a_un_residente_le_falta_el_tipo(
        client, db, grupo_abierto, cleaner_user, worker_headers, tipo_higiene):
    """Ni siquiera a medias: el que sí traía tipo también sigue abierto."""
    res = client.post('/api/nfc/finalize-group-care', headers=worker_headers, json={
        'worker_id': cleaner_user.id,
        'record_mapping': [
            {'record_id': grupo_abierto[0].id, 'care_type_ids': [tipo_higiene.id]},
            {'record_id': grupo_abierto[1].id, 'care_type_ids': []},
        ],
    })

    assert res.status_code == 400
    for r in grupo_abierto:
        assert db.session.get(CareRecord, r.id).end_time is None


def test_el_grupo_se_cierra_con_tipo_en_todos(
        client, db, grupo_abierto, cleaner_user, worker_headers, tipo_higiene):
    res = client.post('/api/nfc/finalize-group-care', headers=worker_headers, json={
        'worker_id': cleaner_user.id,
        'record_mapping': [
            {'record_id': r.id, 'care_type_ids': [tipo_higiene.id]}
            for r in grupo_abierto
        ],
    })

    assert res.status_code == 200
    for r in grupo_abierto:
        registro = db.session.get(CareRecord, r.id)
        assert registro.end_time is not None
        assert [c.name for c in registro.care_types] == ['Higiene']
