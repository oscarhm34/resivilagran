"""
test_horario_atenciones.py — Tipos de atención con horario e instrucciones.

Hay atenciones que se hacen siempre a la misma hora: levantar por la mañana,
acostar por la noche. El sistema no lo sabía, así que al terminar la trabajadora
elegía el tipo de una lista donde todo pesaba igual, y no había ningún sitio
donde consultar qué hay que hacer en esa atención.

Lo que se fija aquí:
  - qué tipos toca a cada hora, incluida la franja que cruza la medianoche;
  - que al iniciar la atención llegan las instrucciones;
  - que la sugerencia se calcula con la hora en que EMPEZÓ la atención, no con
    la de cierre;
  - y que un tipo sin horario se comporta exactamente como antes.
"""

from datetime import datetime, time, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import AppSetting, CareRecord, CareType, Resident
from app.utils import _tipos_de_atencion_a_esta_hora


@pytest.fixture(autouse=True)
def sin_minimo(db):
    """La duración mínima es otra cosa; aquí estorba."""
    AppSetting.set('min_session_seconds', '0')
    yield
    AppSetting.set('min_session_seconds', '60')


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def residente(db):
    r = Resident(name='Josefa Ruiz', nfc_code='RES-HOR-1', active=True)
    db.session.add(r)
    db.session.commit()
    return r


def _tipo(db, nombre, inicio=None, fin=None, instrucciones=None, activo=True):
    ct = CareType(name=nombre, active=activo, instructions=instrucciones,
                  start_time=inicio, end_time=fin)
    db.session.add(ct)
    db.session.commit()
    return ct


def _en(h, m=0):
    return datetime(2026, 9, 1, h, m)


# ── El helper ────────────────────────────────────────────────────────────────

def test_dentro_de_la_franja_el_tipo_toca(db):
    _tipo(db, 'Levantar', time(7, 0), time(11, 0))

    assert [t.name for t in _tipos_de_atencion_a_esta_hora(_en(9))] == ['Levantar']


def test_fuera_de_la_franja_no_toca(db):
    _tipo(db, 'Levantar', time(7, 0), time(11, 0))

    assert _tipos_de_atencion_a_esta_hora(_en(15)) == []


def test_los_bordes_de_la_franja_entran(db):
    _tipo(db, 'Levantar', time(7, 0), time(11, 0))

    assert _tipos_de_atencion_a_esta_hora(_en(7, 0))
    assert _tipos_de_atencion_a_esta_hora(_en(11, 0))


def test_una_franja_que_cruza_la_medianoche_funciona(db):
    """«Acostar» de 21:00 a 01:00: sin esto habria que partirla en dos."""
    _tipo(db, 'Acostar', time(21, 0), time(1, 0))

    assert _tipos_de_atencion_a_esta_hora(_en(22))      # antes de medianoche
    assert _tipos_de_atencion_a_esta_hora(_en(0, 30))   # despues
    assert not _tipos_de_atencion_a_esta_hora(_en(12))  # a mediodia no


def test_un_tipo_sin_horario_no_se_sugiere_nunca(db):
    """Los 23 tipos que ya existen no cambian de comportamiento."""
    _tipo(db, 'Otro')

    assert _tipos_de_atencion_a_esta_hora(_en(9)) == []
    assert _tipos_de_atencion_a_esta_hora(_en(23)) == []


def test_un_tipo_inactivo_no_se_sugiere(db):
    _tipo(db, 'Levantar', time(7, 0), time(11, 0), activo=False)

    assert _tipos_de_atencion_a_esta_hora(_en(9)) == []


def test_si_coinciden_varios_se_devuelven_todos(db):
    """No hay motivo para elegir por la trabajadora cual de los dos era."""
    _tipo(db, 'Levantar', time(7, 0), time(11, 0))
    _tipo(db, 'Aseo', time(8, 0), time(10, 0))

    assert len(_tipos_de_atencion_a_esta_hora(_en(9))) == 2


# ── Al iniciar la atención ───────────────────────────────────────────────────

def test_al_iniciar_llegan_las_instrucciones_de_lo_que_toca(
        client, db, residente, cleaner_user, worker_headers):
    ahora = datetime.now()
    _tipo(db, 'Levantar', time(0, 0), time(23, 59),
          instrucciones='Subir la persiana, asear y vestir.')

    res = client.post('/api/nfc/scan', headers=worker_headers,
                      json={'nfc_code': residente.nfc_code, 'mode': 'care',
                            'worker_id': cleaner_user.id})

    datos = res.get_json()
    assert datos['action'] == 'started'
    assert datos['care_hint']['name'] == 'Levantar'
    assert 'persiana' in datos['care_hint']['instructions']
    assert datos['subject_sub'] == 'Levantar'


def test_sin_ningun_tipo_en_horario_no_se_manda_nada(
        client, db, residente, cleaner_user, worker_headers):
    """Sin horario configurado, la trabajadora ve exactamente lo de siempre."""
    _tipo(db, 'Otro')

    res = client.post('/api/nfc/scan', headers=worker_headers,
                      json={'nfc_code': residente.nfc_code, 'mode': 'care',
                            'worker_id': cleaner_user.id})

    assert 'care_hint' not in res.get_json()


def test_un_tipo_en_horario_pero_sin_instrucciones_se_anuncia_igual(
        client, db, residente, cleaner_user, worker_headers):
    """Saber que toca ya sirve, aunque nadie haya escrito los pasos."""
    _tipo(db, 'Levantar', time(0, 0), time(23, 59))

    res = client.post('/api/nfc/scan', headers=worker_headers,
                      json={'nfc_code': residente.nfc_code, 'mode': 'care',
                            'worker_id': cleaner_user.id})

    hint = res.get_json()['care_hint']
    assert hint['name'] == 'Levantar'
    assert hint['instructions'] is None


# ── Al finalizar ─────────────────────────────────────────────────────────────

def test_al_finalizar_viene_sugerido_el_tipo_de_esa_hora(
        client, db, residente, cleaner_user, worker_headers):
    ct = _tipo(db, 'Levantar', time(0, 0), time(23, 59))
    rec = CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                     start_time=datetime.now() - timedelta(minutes=5))
    db.session.add(rec)
    db.session.commit()

    res = client.post('/api/nfc/scan', headers=worker_headers,
                      json={'nfc_code': residente.nfc_code, 'mode': 'care',
                            'worker_id': cleaner_user.id})

    datos = res.get_json()
    assert datos['action'] == 'select_care_type_end'
    assert datos['suggested_care_type_ids'] == [ct.id]


def test_la_sugerencia_usa_la_hora_de_inicio_no_la_de_cierre(
        client, db, residente, cleaner_user, worker_headers):
    """Quien abre a las 10:55 y cierra a las 11:10 estuvo levantando."""
    ahora = datetime.now()
    inicio = ahora - timedelta(hours=2)
    levantar = _tipo(db, 'Levantar',
                     (inicio - timedelta(minutes=30)).time(),
                     (inicio + timedelta(minutes=30)).time())
    _tipo(db, 'Comida', (ahora - timedelta(minutes=5)).time(),
          (ahora + timedelta(minutes=30)).time())
    rec = CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                     start_time=inicio)
    db.session.add(rec)
    db.session.commit()

    res = client.post('/api/nfc/scan', headers=worker_headers,
                      json={'nfc_code': residente.nfc_code, 'mode': 'care',
                            'worker_id': cleaner_user.id})

    assert res.get_json()['suggested_care_type_ids'] == [levantar.id]


def test_sin_horarios_la_lista_de_sugerencias_viene_vacia(
        client, db, residente, cleaner_user, worker_headers):
    _tipo(db, 'Otro')
    rec = CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                     start_time=datetime.now() - timedelta(minutes=5))
    db.session.add(rec)
    db.session.commit()

    res = client.post('/api/nfc/scan', headers=worker_headers,
                      json={'nfc_code': residente.nfc_code, 'mode': 'care',
                            'worker_id': cleaner_user.id})

    assert res.get_json()['suggested_care_type_ids'] == []


def test_la_webapp_recibe_las_instrucciones_en_la_lista_de_tipos(
        client, db, worker_headers):
    _tipo(db, 'Levantar', time(7, 0), time(11, 0), instrucciones='Asear y vestir.')

    res = client.get('/api/care-types', headers=worker_headers)

    assert res.get_json()[0]['instructions'] == 'Asear y vestir.'


# ── El formulario del panel ──────────────────────────────────────────────────

def test_el_panel_guarda_horario_e_instrucciones(auth_client, db):
    auth_client.post('/care-types/add_edit', data={
        'name': 'Levantar', 'start_time': '07:00', 'end_time': '11:00',
        'instructions': 'Subir la persiana, asear y vestir.',
    }, follow_redirects=True)

    ct = CareType.query.filter_by(name='Levantar').one()
    assert ct.start_time == time(7, 0)
    assert ct.end_time == time(11, 0)
    assert 'persiana' in ct.instructions


def test_con_una_sola_hora_avisa_y_no_guarda_horario(auth_client, db):
    """Media franja no significa nada: el tipo no se sugeriria nunca."""
    res = auth_client.post('/care-types/add_edit', data={
        'name': 'Levantar', 'start_time': '07:00', 'end_time': '',
    }, follow_redirects=True)

    ct = CareType.query.filter_by(name='Levantar').one()
    assert ct.start_time is None and ct.end_time is None
    assert 'hora de inicio y hora de fin' in res.get_data(as_text=True)


def test_una_hora_mal_formada_no_revienta(auth_client, db):
    res = auth_client.post('/care-types/add_edit', data={
        'name': 'Levantar', 'start_time': 'a media mañana', 'end_time': '11:00',
    }, follow_redirects=True)

    assert res.status_code == 200
    assert CareType.query.filter_by(name='Levantar').one().start_time is None


def test_editar_un_tipo_puede_quitarle_el_horario(auth_client, db):
    ct = _tipo(db, 'Levantar', time(7, 0), time(11, 0), instrucciones='Algo')

    auth_client.post('/care-types/add_edit', data={
        'care_type_id': ct.id, 'name': 'Levantar',
        'start_time': '', 'end_time': '', 'instructions': '',
    }, follow_redirects=True)

    ct = db.session.get(CareType, ct.id)
    assert ct.start_time is None and ct.instructions is None


def test_guardar_el_horario_queda_en_la_auditoria(auth_client, db):
    from app.models import AuditLog

    auth_client.post('/care-types/add_edit', data={
        'name': 'Acostar', 'start_time': '21:00', 'end_time': '01:00',
        'instructions': 'Poner el pijama.',
    }, follow_redirects=True)

    registro = AuditLog.query.filter_by(table_name='care_type').order_by(
        AuditLog.id.desc()).first()
    assert '21:00' in (registro.details or '')
