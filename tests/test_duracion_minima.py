"""
test_duracion_minima.py — No se puede cerrar una sesión recién abierta.

Una trabajadora escaneaba la etiqueta de una habitación o de un residente y la
volvía a cerrar dos segundos después. Quedaba un registro de limpieza o de
atención con su hora de inicio, su hora de fin y una duración de un segundo,
indistinguible de uno real para las estadísticas, los informes de rendimiento y
la trazabilidad de qué se hizo con cada residente.

Hay siete caminos por los que una trabajadora puede cerrar una sesión. Aquí se
comprueban todos, porque tapar seis no sirve de nada.

Los cierres administrativos —el botón del panel y el barrido de sesiones
colgadas a las 24 h— quedan fuera a propósito: son la válvula de escape cuando
algo se queda atascado.
"""

from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import (AppSetting, CareRecord, CleaningRecord, CareType,
                        Cleaner, Resident, Room)


MINIMO = 60


@pytest.fixture(autouse=True)
def minimo_por_defecto(db):
    """Fija el mínimo explícitamente: los tests no dependen del valor guardado."""
    AppSetting.set('min_session_seconds', str(MINIMO))
    yield
    AppSetting.set('min_session_seconds', str(MINIMO))


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def limpieza_recien_abierta(db, cleaner_user, room):
    r = CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                       start_time=datetime.now())
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def residente(db, room):
    r = Resident(name='Josefa Ruiz', nfc_code='RES-MIN-1', active=True,
                 room_number=room.number)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def atencion_recien_abierta(db, cleaner_user, residente):
    r = CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                   start_time=datetime.now())
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def tipo_atencion(db):
    ct = CareType(name='Higiene')
    db.session.add(ct)
    db.session.commit()
    return ct


def _envejecer(db, registro, minutos=10):
    registro.start_time = datetime.now() - timedelta(minutes=minutos)
    db.session.commit()
    return registro


# ── Los siete caminos ────────────────────────────────────────────────────────

def test_el_segundo_escaneo_de_la_api_antigua_no_cierra_al_instante(
        client, db, limpieza_recien_abierta, cleaner_user, room, worker_headers):
    """`/start_cleaning` cierra si ya hay sesión abierta: es un cierre más."""
    res = client.post('/start_cleaning', headers=worker_headers,
                      json={'room_id': room.number})

    assert res.status_code == 409
    assert res.get_json()['seconds_left'] > 0
    assert db.session.get(CleaningRecord, limpieza_recien_abierta.id).end_time is None


def test_end_cleaning_no_cierra_al_instante(
        client, db, limpieza_recien_abierta, worker_headers):
    res = client.post('/end_cleaning', headers=worker_headers,
                      json={'record_id': limpieza_recien_abierta.id})

    assert res.status_code == 409
    assert db.session.get(CleaningRecord, limpieza_recien_abierta.id).end_time is None


def test_reescanear_la_etiqueta_no_cierra_al_instante(
        client, db, limpieza_recien_abierta, cleaner_user, room, worker_headers):
    res = client.post('/api/nfc/scan', headers=worker_headers,
                      json={'nfc_code': room.number, 'mode': 'cleaning',
                            'worker_id': cleaner_user.id})

    assert res.status_code == 409
    assert 'Faltan' in res.get_json()['error']
    assert db.session.get(CleaningRecord, limpieza_recien_abierta.id).end_time is None


def test_un_escaneo_rechazado_no_abre_una_segunda_sesion(
        client, db, limpieza_recien_abierta, cleaner_user, room, worker_headers):
    """Si el rechazo saliera fuera del `if`, caeria a abrir otra limpieza."""
    client.post('/api/nfc/scan', headers=worker_headers,
                json={'nfc_code': room.number, 'mode': 'cleaning',
                      'worker_id': cleaner_user.id})

    assert CleaningRecord.query.count() == 1


def test_el_escaneo_de_un_residente_rechaza_antes_de_pedir_el_tipo(
        client, db, atencion_recien_abierta, cleaner_user, residente, worker_headers):
    """Rellenar tipo y constantes para que te lo rechacen al final seria peor."""
    res = client.post('/api/nfc/scan', headers=worker_headers,
                      json={'nfc_code': residente.nfc_code, 'mode': 'care',
                            'worker_id': cleaner_user.id})

    assert res.status_code == 409
    assert 'action' not in res.get_json()


def test_el_boton_manual_de_limpieza_rechaza_antes_del_checklist(
        client, db, limpieza_recien_abierta, cleaner_user, worker_headers):
    AppSetting.set('nfc_only', 'false')

    res = client.post('/api/nfc/end-session', headers=worker_headers,
                      json={'record_id': limpieza_recien_abierta.id,
                            'worker_id': cleaner_user.id, 'mode': 'cleaning'})

    AppSetting.set('nfc_only', 'true')
    assert res.status_code == 409
    assert 'action' not in res.get_json()


def test_el_boton_manual_de_atencion_rechaza_antes_de_pedir_el_tipo(
        client, db, atencion_recien_abierta, cleaner_user, worker_headers):
    AppSetting.set('nfc_only', 'false')

    res = client.post('/api/nfc/end-session', headers=worker_headers,
                      json={'record_id': atencion_recien_abierta.id,
                            'worker_id': cleaner_user.id, 'mode': 'care'})

    AppSetting.set('nfc_only', 'true')
    assert res.status_code == 409
    assert 'action' not in res.get_json()


def test_finalizar_la_atencion_no_cierra_al_instante(
        client, db, atencion_recien_abierta, cleaner_user, tipo_atencion, worker_headers):
    """Se vuelve a comprobar aqui: la pantalla puede llevar un rato abierta."""
    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_recien_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [tipo_atencion.id],
    })

    assert res.status_code == 409
    assert db.session.get(CareRecord, atencion_recien_abierta.id).end_time is None


def test_finalizar_la_limpieza_no_cierra_al_instante(
        client, db, limpieza_recien_abierta, cleaner_user, worker_headers):
    res = client.post('/api/nfc/finalize-cleaning', headers=worker_headers, json={
        'record_id': limpieza_recien_abierta.id,
        'worker_id': cleaner_user.id,
        'checklist': [],
    })

    assert res.status_code == 409
    assert db.session.get(CleaningRecord, limpieza_recien_abierta.id).end_time is None


def test_el_aviso_del_grupo_dice_a_quien_le_falta(
        client, db, cleaner_user, tipo_atencion, worker_headers):
    """Con ocho residentes, "espera un poco" no dice donde mirar."""
    res_ = Resident(name='Pilar Gomez', nfc_code='RES-GRP-AVISO', active=True)
    db.session.add(res_)
    db.session.flush()
    rec = CareRecord(worker_id=cleaner_user.id, resident_id=res_.id,
                     start_time=datetime.now())
    db.session.add(rec)
    db.session.commit()

    res = client.post('/api/nfc/finalize-group-care', headers=worker_headers, json={
        'worker_id': cleaner_user.id,
        'record_mapping': [{'record_id': rec.id, 'care_type_ids': [tipo_atencion.id]}],
    })

    assert 'Pilar Gomez' in res.get_json()['error']


def test_el_rechazo_lleva_un_codigo_reconocible(
        client, db, limpieza_recien_abierta, worker_headers):
    res = client.post('/end_cleaning', headers=worker_headers,
                      json={'record_id': limpieza_recien_abierta.id})

    assert res.get_json()['code'] == 'MIN_DURATION'


def test_el_grupo_no_se_cierra_si_a_un_solo_residente_le_falta_tiempo(
        client, db, cleaner_user, tipo_atencion, worker_headers):
    """Cerrar media atencion grupal dejaria la otra mitad colgada."""
    registros = []
    for i, nombre in enumerate(('Pilar Gomez', 'Antonio Diaz')):
        res_ = Resident(name=nombre, nfc_code=f'RES-GRP-MIN-{i}', active=True)
        db.session.add(res_)
        db.session.flush()
        rec = CareRecord(worker_id=cleaner_user.id, resident_id=res_.id,
                         start_time=datetime.now())
        db.session.add(rec)
        registros.append(rec)
    db.session.commit()
    _envejecer(db, registros[0])          # este si podria cerrarse

    res = client.post('/api/nfc/finalize-group-care', headers=worker_headers, json={
        'worker_id': cleaner_user.id,
        'record_mapping': [{'record_id': r.id, 'care_type_ids': [tipo_atencion.id]}
                           for r in registros],
    })

    assert res.status_code == 409
    for r in registros:
        assert db.session.get(CareRecord, r.id).end_time is None


# ── Pasado el mínimo, todo sigue funcionando ────────────────────────────────

def test_pasado_el_minimo_la_limpieza_se_cierra(
        client, db, limpieza_recien_abierta, cleaner_user, worker_headers):
    _envejecer(db, limpieza_recien_abierta)

    res = client.post('/api/nfc/finalize-cleaning', headers=worker_headers, json={
        'record_id': limpieza_recien_abierta.id,
        'worker_id': cleaner_user.id,
        'checklist': [],
    })

    assert res.status_code == 200
    assert db.session.get(CleaningRecord, limpieza_recien_abierta.id).end_time is not None


def test_pasado_el_minimo_la_atencion_se_cierra(
        client, db, atencion_recien_abierta, cleaner_user, tipo_atencion, worker_headers):
    _envejecer(db, atencion_recien_abierta)

    res = client.post('/api/nfc/finalize-care', headers=worker_headers, json={
        'record_id': atencion_recien_abierta.id,
        'worker_id': cleaner_user.id,
        'care_type_ids': [tipo_atencion.id],
    })

    assert res.status_code == 200
    assert db.session.get(CareRecord, atencion_recien_abierta.id).end_time is not None


# ── Casos límite ─────────────────────────────────────────────────────────────

def test_con_el_ajuste_a_cero_no_se_comprueba_nada(
        client, db, limpieza_recien_abierta, cleaner_user, worker_headers):
    """La salida si un dia estorba, sin desplegar nada."""
    AppSetting.set('min_session_seconds', '0')

    res = client.post('/api/nfc/finalize-cleaning', headers=worker_headers, json={
        'record_id': limpieza_recien_abierta.id,
        'worker_id': cleaner_user.id,
        'checklist': [],
    })

    assert res.status_code == 200


def test_una_sesion_sin_hora_de_inicio_se_puede_cerrar(
        client, db, cleaner_user, room, worker_headers):
    """`CleaningRecord.start_time` es nullable: bloquearla la dejaria atrapada."""
    r = CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id, start_time=None)
    db.session.add(r)
    db.session.commit()

    res = client.post('/end_cleaning', headers=worker_headers, json={'record_id': r.id})

    assert res.status_code == 200


def test_el_aviso_dice_cuanto_falta(
        client, db, limpieza_recien_abierta, worker_headers):
    res = client.post('/end_cleaning', headers=worker_headers,
                      json={'record_id': limpieza_recien_abierta.id})

    datos = res.get_json()
    assert 'Faltan 1:00' in datos['error'] or 'Faltan 0:5' in datos['error']
    assert 0 < datos['seconds_left'] <= MINIMO


# ── Los cierres administrativos no se tocan ─────────────────────────────────

def test_el_admin_cierra_una_sesion_recien_abierta(
        auth_client, db, limpieza_recien_abierta):
    """Es la valvula de escape cuando algo se queda atascado."""
    auth_client.post(f'/admin/close-session/cleaning/{limpieza_recien_abierta.id}',
                     follow_redirects=True)

    assert db.session.get(CleaningRecord, limpieza_recien_abierta.id).end_time is not None


def test_el_admin_cierra_una_atencion_recien_abierta(
        auth_client, db, atencion_recien_abierta):
    auth_client.post(f'/admin/close-session/care/{atencion_recien_abierta.id}',
                     follow_redirects=True)

    assert db.session.get(CareRecord, atencion_recien_abierta.id).end_time is not None


def test_el_barrido_de_24h_sigue_cerrando_las_colgadas(
        auth_client, db, limpieza_recien_abierta):
    _envejecer(db, limpieza_recien_abierta, minutos=60 * 30)

    auth_client.get('/')          # el barrido corre al cargar la portada

    assert db.session.get(CleaningRecord, limpieza_recien_abierta.id).end_time is not None


# ── El ajuste ────────────────────────────────────────────────────────────────

def test_el_ajuste_se_guarda_desde_el_panel(auth_client, db):
    auth_client.post('/admin/settings', data={'min_session_seconds': '90'},
                     follow_redirects=True)

    assert AppSetting.get('min_session_seconds') == '90'


def test_un_valor_disparatado_se_recorta(auth_client, db):
    auth_client.post('/admin/settings', data={'min_session_seconds': '99999'},
                     follow_redirects=True)

    assert AppSetting.get('min_session_seconds') == '600'


def test_un_valor_no_numerico_cae_al_defecto(auth_client, db):
    auth_client.post('/admin/settings', data={'min_session_seconds': 'lo que sea'},
                     follow_redirects=True)

    assert AppSetting.get('min_session_seconds') == '60'


def test_la_webapp_recibe_el_minimo_en_la_configuracion(client, db, worker_headers):
    """La webapp lo necesita para avisar antes, no despues de intentarlo."""
    res = client.get('/api/config', headers=worker_headers)

    assert res.get_json()['min_session_seconds'] == MINIMO
