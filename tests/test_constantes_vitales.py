"""
test_constantes_vitales.py — Constantes agrupadas: la tensión, en una sola toma.

La sistólica y la diastólica estaban dadas de alta como dos campos sueltos del
mismo tipo de atención, así que se pedían y se mostraban como si fueran dos
constantes distintas. Con una etiqueta de grupo compartida se piden y se
enseñan juntas, 120/80.

Lo que se cubre aquí es que agrupar sea solo presentación: cada valor sigue
guardándose como su propia lectura y el servidor sigue exigiéndolos todos.
"""

from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import (Resident, CareType, CareRecord,
                        VitalSignType, VitalSignReading)


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def tension(db):
    """Tipo de atención con sistólica y diastólica agrupadas, y peso suelto."""
    ct = CareType(name='Constantes vitales', active=True)
    db.session.add(ct)
    db.session.flush()
    sis = VitalSignType(care_type_id=ct.id, name='Sistólica', unit='mmHg',
                        min_value=90, max_value=140, input_type='number',
                        group_label='Tensión arterial', sort_order=1, active=True)
    dia = VitalSignType(care_type_id=ct.id, name='Diastólica', unit='mmHg',
                        min_value=50, max_value=90, input_type='number',
                        group_label='Tensión arterial', sort_order=2, active=True)
    peso = VitalSignType(care_type_id=ct.id, name='Peso', unit='kg',
                         input_type='decimal', sort_order=3, active=True)
    db.session.add_all([sis, dia, peso])
    db.session.commit()
    return {'ct': ct, 'sis': sis, 'dia': dia, 'peso': peso}


@pytest.fixture
def atencion_abierta(db, cleaner_user):
    resident = Resident(name='Antonia Vidal', nfc_code='RES-CV-1', active=True)
    db.session.add(resident)
    db.session.flush()
    r = CareRecord(worker_id=cleaner_user.id, resident_id=resident.id,
                   start_time=datetime.now() - timedelta(minutes=15))
    db.session.add(r)
    db.session.commit()
    return r


# ── La webapp recibe el grupo ────────────────────────────────────────────────

def test_la_api_devuelve_la_etiqueta_de_grupo(client, tension, worker_headers):
    """Sin group_label en el payload la webapp no puede pintarlas juntas."""
    res = client.get('/api/care-types', headers=worker_headers)

    assert res.status_code == 200
    campos = next(t for t in res.get_json() if t['id'] == tension['ct'].id)['vital_fields']
    por_nombre = {c['name']: c for c in campos}
    assert por_nombre['Sistólica']['group_label'] == 'Tensión arterial'
    assert por_nombre['Diastólica']['group_label'] == 'Tensión arterial'
    assert por_nombre['Peso']['group_label'] is None


def test_los_campos_llegan_ordenados(client, tension, worker_headers):
    """Dentro del grupo el orden decide el valor que va delante: 120/80."""
    res = client.get('/api/care-types', headers=worker_headers)

    campos = next(t for t in res.get_json() if t['id'] == tension['ct'].id)['vital_fields']
    assert [c['name'] for c in campos] == ['Sistólica', 'Diastólica', 'Peso']


# ── Agrupar no cambia cómo se guarda ─────────────────────────────────────────

def test_una_tension_agrupada_guarda_dos_lecturas(
        client, db, tension, atencion_abierta, cleaner_user, worker_headers):
    """120/80 son dos lecturas, cada una con su tipo y su valor."""
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [tension['ct'].id],
        'vital_signs': [
            {'vital_sign_type_id': tension['sis'].id, 'value': 120},
            {'vital_sign_type_id': tension['dia'].id, 'value': 80},
            {'vital_sign_type_id': tension['peso'].id, 'value': 62.5},
        ],
    })

    assert res.status_code == 200
    lecturas = VitalSignReading.query.filter_by(
        care_record_id=atencion_abierta.id).all()
    assert len(lecturas) == 3
    valores = {l.vital_sign_type_id: l.value for l in lecturas}
    assert valores[tension['sis'].id] == 120
    assert valores[tension['dia'].id] == 80
    assert valores[tension['peso'].id] == 62.5


def test_falta_la_diastolica_y_se_rechaza(
        client, db, tension, atencion_abierta, cleaner_user, worker_headers):
    """Agrupar no puede relajar la validación: media tensión no vale."""
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [tension['ct'].id],
        'vital_signs': [
            {'vital_sign_type_id': tension['sis'].id, 'value': 120},
            {'vital_sign_type_id': tension['peso'].id, 'value': 62.5},
        ],
    })

    assert res.status_code == 400
    assert 'Diastólica' in res.get_json()['error']
    assert db.session.get(CareRecord, atencion_abierta.id).end_time is None


# ── Cómo se muestra ──────────────────────────────────────────────────────────

def test_constantes_agrupadas_junta_la_tension_y_deja_suelto_el_peso(
        db, tension, atencion_abierta):
    """Test unitario del método, sin pasar por HTTP."""
    db.session.add_all([
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension['dia'].id, value=80),
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension['sis'].id, value=120),
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension['peso'].id, value=62.5),
    ])
    db.session.commit()

    grupos = atencion_abierta.constantes_agrupadas()

    assert grupos == [
        {'name': 'Tensión arterial', 'valores': ['120', '80'], 'unit': 'mmHg'},
        {'name': 'Peso', 'valores': ['62.5'], 'unit': 'kg'},
    ]


def test_registros_atencion_pinta_la_tension_en_una_linea(
        auth_client, db, tension, atencion_abierta):
    atencion_abierta.end_time = datetime.now()
    db.session.add_all([
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension['sis'].id, value=120),
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension['dia'].id, value=80),
    ])
    db.session.commit()

    html = auth_client.get('/registros-atencion').get_data(as_text=True)

    assert 'Tensión arterial: <strong>120/80</strong> mmHg' in html
    assert 'Sistólica:' not in html


# ── Alta y edición del grupo desde el panel ──────────────────────────────────

def test_admin_crea_un_campo_con_grupo(auth_client, db, tension):
    resp = auth_client.post(
        f"/care-types/{tension['ct'].id}/vital-fields/add_edit",
        data={'vf_name': 'Media', 'vf_unit': 'mmHg', 'vf_group': 'Tensión arterial',
              'vf_input_type': 'number', 'vf_sort_order': '4'},
        follow_redirects=True)

    assert resp.status_code == 200
    vf = VitalSignType.query.filter_by(name='Media').first()
    assert vf.group_label == 'Tensión arterial'
    assert vf.sort_order == 4


def test_admin_agrupa_un_campo_que_ya_existia(auth_client, db, tension):
    """El caso real: una sistólica con meses de lecturas, que no se puede
    borrar, tiene que poder agruparse editándola."""
    resp = auth_client.post(
        f"/care-types/{tension['ct'].id}/vital-fields/add_edit",
        data={'vf_id': tension['peso'].id, 'vf_name': 'Peso', 'vf_unit': 'kg',
              'vf_group': '', 'vf_input_type': 'decimal', 'vf_sort_order': '3'},
        follow_redirects=True)

    assert resp.status_code == 200
    assert db.session.get(VitalSignType, tension['peso'].id).group_label is None

    auth_client.post(
        f"/care-types/{tension['ct'].id}/vital-fields/add_edit",
        data={'vf_id': tension['peso'].id, 'vf_name': 'Peso', 'vf_unit': 'kg',
              'vf_group': 'Medidas', 'vf_input_type': 'decimal', 'vf_sort_order': '3'},
        follow_redirects=True)

    assert db.session.get(VitalSignType, tension['peso'].id).group_label == 'Medidas'


def test_los_campos_vitales_exigen_admin(client, tension):
    resp = client.post(f"/care-types/{tension['ct'].id}/vital-fields/add_edit",
                       data={'vf_name': 'X', 'vf_unit': 'y'})

    assert resp.status_code in (302, 401, 403)
    assert VitalSignType.query.filter_by(name='X').first() is None
