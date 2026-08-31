"""
test_tipos_atencion.py — Corregir el tipo de una atención ya cerrada.

Contexto del fallo real (31/08/2026): dos atenciones quedaron guardadas sin
ningún tipo. Los 23 tipos estaban dados de alta y activos, así que la lista le
llegó vacía a la webapp por un fallo puntual de la petición, que dijo "no hay
tipos configurados" y dejó finalizar sin ninguno.

Que eso ya no pueda repetirse lo cubre `test_finalizar_atencion.py`. Aquí se
fija la otra mitad: los registros que ya quedaron sin tipo se pueden corregir
desde el panel, y esa misma vía no sirve para dejar uno sin tipo.
"""

from datetime import datetime, timedelta

import pytest

from app.models import Resident, CareType, CareRecord


@pytest.fixture
def atencion_abierta(db, cleaner_user):
    resident = Resident(name='Josefa Ruiz', nfc_code='RES-TIPO-1', active=True)
    db.session.add(resident)
    db.session.flush()
    r = CareRecord(worker_id=cleaner_user.id, resident_id=resident.id,
                   start_time=datetime.now() - timedelta(minutes=15))
    db.session.add(r)
    db.session.commit()
    return r


# ── Corregir el tipo de un registro ya cerrado ───────────────────────────────

def test_admin_asigna_tipo_a_una_atencion_cerrada_sin_tipo(
        auth_client, db, atencion_abierta):
    atencion_abierta.end_time = datetime.now()
    ct = CareType(name='Higiene', active=True)
    db.session.add(ct)
    db.session.commit()

    res = auth_client.post(f'/admin/care-record/{atencion_abierta.id}/edit',
                          data={'care_type_ids': [str(ct.id)]},
                          follow_redirects=True)

    assert res.status_code == 200
    registro = db.session.get(CareRecord, atencion_abierta.id)
    assert [c.name for c in registro.care_types] == ['Higiene']


def test_admin_sustituye_los_tipos_en_vez_de_acumularlos(
        auth_client, db, atencion_abierta):
    viejo = CareType(name='Higiene', active=True)
    nuevo = CareType(name='Movilizacion', active=True)
    db.session.add_all([viejo, nuevo])
    db.session.commit()
    atencion_abierta.care_types.append(viejo)
    db.session.commit()

    auth_client.post(f'/admin/care-record/{atencion_abierta.id}/edit',
                    data={'care_type_ids': [str(nuevo.id)]},
                    follow_redirects=True)

    registro = db.session.get(CareRecord, atencion_abierta.id)
    assert [c.name for c in registro.care_types] == ['Movilizacion']


def test_editar_una_atencion_exige_ser_admin(client, db, atencion_abierta):
    res = client.post(f'/admin/care-record/{atencion_abierta.id}/edit',
                      data={'care_type_ids': []})

    assert res.status_code in (302, 401, 403)
    assert res.headers.get('Location', '').find('/login') != -1 or res.status_code in (401, 403)


def test_editar_una_atencion_inexistente_no_revienta(auth_client, db):
    res = auth_client.post('/admin/care-record/99999/edit',
                          data={'care_type_ids': []}, follow_redirects=True)

    assert res.status_code == 200


def test_corregir_no_puede_dejar_el_registro_sin_tipo(
        auth_client, db, atencion_abierta):
    """Si no sirviera de nada, seria un agujero por la puerta de atras."""
    ct = CareType(name='Higiene', active=True)
    db.session.add(ct)
    db.session.commit()
    atencion_abierta.care_types.append(ct)
    db.session.commit()

    auth_client.post(f'/admin/care-record/{atencion_abierta.id}/edit',
                    data={'care_type_ids': []}, follow_redirects=True)

    registro = db.session.get(CareRecord, atencion_abierta.id)
    assert [c.name for c in registro.care_types] == ['Higiene']
