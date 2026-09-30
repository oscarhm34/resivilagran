"""
test_reloj_sesiones.py — Las horas que salen al móvil llevan zona.

El cronómetro de la webapp calculaba el tiempo transcurrido restando la hora
del móvil a la hora de inicio que manda el servidor. Eso da por bueno que los
dos relojes coincidan, y cuando el del NAS se desajustó, una atención recién
abierta salía con casi una hora ya contada.

El cliente corrige ahora el desfase con la cabecera `Date` de cada respuesta.
Aquí se fija la otra mitad: que las horas viajen con su desplazamiento
explícito, para que un móvil con la zona horaria mal puesta tampoco pueda
interpretarlas en el huso que no toca.

Y la regresión concreta: `stale` se calculaba volviendo a parsear ese mismo
texto, lo que con zona dentro deja de poder restarse de un `datetime.now()`
naive.
"""

from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import AppSetting, CareRecord, CleaningRecord, Resident
from app.utils import _iso_con_zona


@pytest.fixture(autouse=True)
def sin_minimo(db):
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
    r = Resident(name='Pilar Nadal', nfc_code='RES-REL-1', active=True)
    db.session.add(r)
    db.session.commit()
    return r


# ── El helper ────────────────────────────────────────────────────────────────

def test_el_iso_lleva_desplazamiento():
    texto = _iso_con_zona(datetime(2026, 9, 30, 10, 5, 33))

    assert datetime.fromisoformat(texto).utcoffset() is not None


def test_el_iso_no_mueve_el_instante():
    momento = datetime(2026, 9, 30, 10, 5, 33)

    vuelta = datetime.fromisoformat(_iso_con_zona(momento))

    assert vuelta.replace(tzinfo=None) == momento


def test_una_fecha_de_invierno_lleva_su_propio_desplazamiento():
    """Enero y julio no tienen el mismo desfase: se resuelve por fecha."""
    invierno = datetime.fromisoformat(_iso_con_zona(datetime(2026, 1, 15, 12, 0)))
    verano = datetime.fromisoformat(_iso_con_zona(datetime(2026, 7, 15, 12, 0)))

    assert invierno.utcoffset() != verano.utcoffset()


def test_sin_fecha_no_hay_texto():
    assert _iso_con_zona(None) is None


# ── Lo que sale por la API ───────────────────────────────────────────────────

def test_al_abrir_una_atencion_la_hora_lleva_zona(client, db, residente,
                                                  cleaner_user, worker_headers):
    res = client.post('/api/nfc/scan', json={
        'nfc_code': residente.nfc_code, 'worker_id': cleaner_user.id,
        'mode': 'care', 'confirm': True,
    }, headers=worker_headers)

    assert res.status_code == 200
    inicio = res.get_json()['start_time']
    assert datetime.fromisoformat(inicio).utcoffset() is not None


def test_la_hora_que_sale_es_la_que_se_guardo(client, db, residente,
                                              cleaner_user, worker_headers):
    res = client.post('/api/nfc/scan', json={
        'nfc_code': residente.nfc_code, 'worker_id': cleaner_user.id,
        'mode': 'care', 'confirm': True,
    }, headers=worker_headers)

    devuelta = datetime.fromisoformat(res.get_json()['start_time'])
    guardada = CareRecord.query.filter_by(end_time=None).one().start_time
    assert devuelta.replace(tzinfo=None) == guardada


def test_las_sesiones_activas_llevan_zona(client, db, residente, cleaner_user,
                                          worker_headers):
    db.session.add(CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                              start_time=datetime.now() - timedelta(minutes=5)))
    db.session.commit()

    sesiones = client.get('/api/worker/active-sessions',
                          headers=worker_headers).get_json()

    assert len(sesiones) == 1
    assert datetime.fromisoformat(sesiones[0]['start_time']).utcoffset() is not None


def test_una_limpieza_activa_tambien_lleva_zona(client, db, room, cleaner_user,
                                                worker_headers):
    db.session.add(CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                                  start_time=datetime.now() - timedelta(minutes=5)))
    db.session.commit()

    sesiones = client.get('/api/worker/active-sessions',
                          headers=worker_headers).get_json()

    assert datetime.fromisoformat(sesiones[0]['start_time']).utcoffset() is not None


# ── La regresión: marcar las sesiones olvidadas ──────────────────────────────

def test_una_sesion_de_hace_horas_sale_marcada(client, db, residente,
                                               cleaner_user, worker_headers):
    AppSetting.set('session_max_minutes', '120')
    db.session.add(CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                              start_time=datetime.now() - timedelta(hours=3)))
    db.session.commit()

    sesion = client.get('/api/worker/active-sessions',
                        headers=worker_headers).get_json()[0]

    assert sesion['stale'] is True
    assert sesion['elapsed_minutes'] == 180


def test_una_sesion_recien_abierta_no_sale_marcada(client, db, residente,
                                                   cleaner_user, worker_headers):
    AppSetting.set('session_max_minutes', '120')
    db.session.add(CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                              start_time=datetime.now()))
    db.session.commit()

    sesion = client.get('/api/worker/active-sessions',
                        headers=worker_headers).get_json()[0]

    assert 'stale' not in sesion
    assert 'elapsed_minutes' not in sesion
