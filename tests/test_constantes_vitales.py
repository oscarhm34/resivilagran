"""
test_constantes_vitales.py — Constantes agrupadas: la tensión, en una sola toma.

La sistólica y la diastólica estaban dadas de alta como dos campos sueltos del
mismo tipo de atención, así que se pedían y se mostraban como si fueran dos
constantes distintas. Con una etiqueta de grupo compartida se piden y se
enseñan juntas, 120/80.

Lo que se cubre aquí es que agrupar sea solo presentación: cada valor sigue
guardándose como su propia lectura y el servidor sigue exigiéndolos todos.
"""

import json
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


# ── Graficas del resumen del residente ───────────────────────────────────────

def _vital_data(html: str) -> list:
    """Las graficas tal como le llegan a Chart.js desde la plantilla."""
    marca = 'const vitalData = '
    inicio = html.index(marca) + len(marca)
    fin = html.index(';' + chr(10), inicio)
    return json.loads(html[inicio:fin])


def test_la_grafica_junta_sistolica_y_diastolica(auth_client, db, tension,
                                                 atencion_abierta, cleaner_user):
    """Una sola grafica "Tension arterial" con dos lineas, no dos graficas."""
    otra = CareRecord(worker_id=cleaner_user.id,
                      resident_id=atencion_abierta.resident_id,
                      start_time=datetime.now() - timedelta(days=1),
                      end_time=datetime.now() - timedelta(days=1))
    db.session.add(otra)
    db.session.flush()
    atencion_abierta.end_time = datetime.now()
    db.session.add_all([
        VitalSignReading(care_record_id=otra.id,
                         vital_sign_type_id=tension['sis'].id, value=130),
        VitalSignReading(care_record_id=otra.id,
                         vital_sign_type_id=tension['dia'].id, value=85),
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension['sis'].id, value=172),
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension['dia'].id, value=98),
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension['peso'].id, value=62.5),
    ])
    db.session.commit()

    resp = auth_client.get(f'/admin/resident/{atencion_abierta.resident_id}')
    assert resp.status_code == 200

    graficas = _vital_data(resp.get_data(as_text=True))
    por_nombre = {g['name']: g for g in graficas}
    # La tension es UNA grafica con dos lineas, y el peso va aparte.
    assert sorted(por_nombre) == ['Peso', 'Tensión arterial']
    tension_chart = por_nombre['Tensión arterial']
    assert [x['name'] for x in tension_chart['series']] == ['Sistólica', 'Diastólica']
    assert [x['data'] for x in tension_chart['series']] == [[130.0, 172.0], [85.0, 98.0]]
    assert len(tension_chart['labels']) == 2
    assert len(por_nombre['Peso']['series']) == 1


def test_la_grafica_agrupada_alinea_los_huecos(db, tension, atencion_abierta,
                                               cleaner_user):
    """Si una toma solo trae la sistolica, la diastolica no se desplaza."""
    from app.blueprints.residents import _grafica_de_constante

    t1 = datetime(2026, 9, 1, 10, 0)
    t2 = datetime(2026, 9, 2, 10, 0)
    grafica = _grafica_de_constante({
        'name': 'Tensión arterial',
        'unit': 'mmHg',
        'momentos': {t1: True, t2: True},
        'series': {
            1: {'name': 'Sistólica', 'orden': (0, 1),
                'min_value': 90, 'max_value': 140,
                'valores': {t1: 130.0, t2: 172.0}},
            2: {'name': 'Diastólica', 'orden': (0, 2),
                'min_value': 50, 'max_value': 90,
                'valores': {t2: 98.0}},
        },
    })

    assert grafica['name'] == 'Tensión arterial'
    assert len(grafica['series']) == 2
    assert grafica['labels'] == ['01/09/2026 10:00', '02/09/2026 10:00']
    sis, dia = grafica['series']
    assert sis['data'] == [130.0, 172.0]
    assert sis['delta'] == 42.0
    # El hueco va a None, no se adelanta el valor del dia siguiente.
    assert dia['data'] == [None, 98.0]
    assert dia['last'] == 98.0
    assert dia['delta'] is None


# ── El caso real de produccion: los dos campos con el mismo orden ────────────

@pytest.fixture
def tension_sin_orden(db):
    """Como estaba en el NAS: sistólica y diastólica, ambas con sort_order 0.

    Con el empate, ordenar por nombre ponía "Diastólica" delante y la tensión
    se leía 80/120. El desempate es el alta, no el alfabeto.
    """
    ct = CareType(name='TENSIÓN ARTERIAL', active=True)
    db.session.add(ct)
    db.session.flush()
    sis = VitalSignType(care_type_id=ct.id, name='Sistólica', unit='mmHg',
                        input_type='number', group_label='Tensión arterial',
                        sort_order=0, active=True)
    db.session.add(sis)
    db.session.flush()          # la sistólica se da de alta primero
    dia = VitalSignType(care_type_id=ct.id, name='Diastólica', unit='mmHg',
                        input_type='number', group_label='Tensión arterial',
                        sort_order=0, active=True)
    db.session.add(dia)
    db.session.commit()
    return {'ct': ct, 'sis': sis, 'dia': dia}


def test_con_el_mismo_orden_manda_el_alta_en_la_webapp(
        client, tension_sin_orden, worker_headers):
    res = client.get('/api/care-types', headers=worker_headers)

    campos = next(t for t in res.get_json()
                  if t['id'] == tension_sin_orden['ct'].id)['vital_fields']
    assert [c['name'] for c in campos] == ['Sistólica', 'Diastólica']


def test_con_el_mismo_orden_el_listado_lee_120_80(
        db, tension_sin_orden, atencion_abierta):
    db.session.add_all([
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension_sin_orden['dia'].id, value=80),
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension_sin_orden['sis'].id, value=120),
    ])
    db.session.commit()

    grupos = atencion_abierta.constantes_agrupadas()

    assert grupos == [
        {'name': 'Tensión arterial', 'valores': ['120', '80'], 'unit': 'mmHg'},
    ]


def test_con_el_mismo_orden_la_grafica_pinta_primero_la_sistolica(
        auth_client, db, tension_sin_orden, atencion_abierta):
    atencion_abierta.end_time = datetime.now()
    db.session.add_all([
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension_sin_orden['dia'].id, value=95),
        VitalSignReading(care_record_id=atencion_abierta.id,
                         vital_sign_type_id=tension_sin_orden['sis'].id, value=160),
    ])
    db.session.commit()

    resp = auth_client.get(f'/admin/resident/{atencion_abierta.resident_id}')
    graficas = _vital_data(resp.get_data(as_text=True))

    assert len(graficas) == 1
    assert [x['name'] for x in graficas[0]['series']] == ['Sistólica', 'Diastólica']
    assert [x['last_display'] for x in graficas[0]['series']] == ['160', '95']
