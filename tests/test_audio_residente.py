"""
test_audio_residente.py — Información del residente e instrucciones, con audio.

Al escanear a un residente la trabajadora tiene que ver lo importante de esa
persona antes de empezar, y poder escucharlo: muchas leen con dificultad o van
con las manos ocupadas.

Lo que se fija aquí:
  - que el audio se genera una sola vez por texto y se reutiliza (cada
    generación cuesta dinero de la cuenta de OpenAI);
  - que **al cambiar el texto suena el texto nuevo**, que es lo que distingue
    esto de formación, donde el audio se queda desincronizado hasta que un
    administrador lo regenera a mano;
  - que sin clave configurada nada revienta y la webapp no ofrece el botón;
  - y que el texto lo elige el servidor, nunca el cliente.
"""

import os
from datetime import datetime, time, timedelta
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from app.models import AppSetting, CareRecord, CareType, Resident

MP3 = b'ID3\x03\x00\x00\x00fake-mp3'


@pytest.fixture(autouse=True)
def sin_minimo(db):
    AppSetting.set('min_session_seconds', '0')
    yield
    AppSetting.set('min_session_seconds', '60')


@pytest.fixture
def con_clave(app):
    """La clave de OpenAI no está puesta en los tests; aquí hace falta."""
    anterior = app.config.get('OPENAI_API_KEY')
    app.config['OPENAI_API_KEY'] = 'sk-de-prueba'
    yield
    app.config['OPENAI_API_KEY'] = anterior


@pytest.fixture
def voz(con_clave):
    """Sustituye la llamada al servicio de voz; ningún test sale a la red."""
    with patch('app.utils._tts_mp3', return_value=(MP3, None)) as m:
        yield m


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def residente(db):
    r = Resident(name='Josefa Ruiz', nfc_code='RES-AUD-1', active=True,
                 relevant_info='Es sorda del oído derecho. Háblale por el izquierdo.')
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture(autouse=True)
def limpiar_audios(app):
    """Los mp3 de un test no pueden hacer pasar al siguiente."""
    yield
    for sub in ('resident_audio', 'care_audio'):
        carpeta = os.path.join(app.config['UPLOAD_FOLDER'], sub)
        if os.path.isdir(carpeta):
            for f in os.listdir(carpeta):
                os.remove(os.path.join(carpeta, f))


def _tipo(db, nombre, inicio=None, fin=None, instrucciones=None):
    ct = CareType(name=nombre, active=True, instructions=instrucciones,
                  start_time=inicio, end_time=fin)
    db.session.add(ct)
    db.session.commit()
    return ct


TODO_EL_DIA = (time(0, 0), time(23, 59))


# ── El audio del residente ───────────────────────────────────────────────────

def test_devuelve_el_mp3_de_la_informacion_del_residente(
        client, residente, worker_headers, voz):
    res = client.get(f'/api/resident/{residente.id}/audio', headers=worker_headers)

    assert res.status_code == 200
    assert res.mimetype == 'audio/mpeg'
    assert res.data == MP3
    assert 'sorda' in voz.call_args[0][0]


def test_el_mismo_texto_no_se_vuelve_a_generar(
        client, residente, worker_headers, voz):
    """Cada generación se paga: pedirlo dos veces debe costar una."""
    client.get(f'/api/resident/{residente.id}/audio', headers=worker_headers)
    client.get(f'/api/resident/{residente.id}/audio', headers=worker_headers)

    assert voz.call_count == 1


def test_al_cambiar_el_texto_se_genera_otro_y_desaparece_el_viejo(
        client, db, residente, worker_headers, voz, app):
    """Nunca se puede oír una versión antigua de la información."""
    client.get(f'/api/resident/{residente.id}/audio', headers=worker_headers)
    residente.relevant_info = 'Ahora usa andador. No la dejes sola de pie.'
    db.session.commit()

    voz.return_value = (b'ID3-nuevo', None)
    res = client.get(f'/api/resident/{residente.id}/audio', headers=worker_headers)

    assert voz.call_count == 2
    assert 'andador' in voz.call_args[0][0]
    assert res.data == b'ID3-nuevo'
    carpeta = os.path.join(app.config['UPLOAD_FOLDER'], 'resident_audio')
    assert len(os.listdir(carpeta)) == 1, 'el audio anterior debería borrarse'


def test_un_residente_sin_informacion_no_llama_al_servicio(
        client, db, worker_headers, voz):
    r = Resident(name='Sin datos', nfc_code='RES-AUD-2', active=True)
    db.session.add(r)
    db.session.commit()

    res = client.get(f'/api/resident/{r.id}/audio', headers=worker_headers)

    assert res.status_code == 404
    assert voz.call_count == 0


def test_un_residente_que_no_existe_da_404(client, worker_headers, voz):
    assert client.get('/api/resident/99999/audio',
                      headers=worker_headers).status_code == 404


def test_el_texto_largo_se_corta_antes_de_mandarlo(
        client, db, residente, worker_headers, voz):
    """Sin tope, un texto de tres folios se cobra entero cada vez que cambie."""
    residente.relevant_info = 'a' * 5000
    db.session.commit()

    client.get(f'/api/resident/{residente.id}/audio', headers=worker_headers)

    assert len(voz.call_args[0][0]) == 2000


def test_sin_clave_de_openai_responde_503(client, residente, worker_headers):
    res = client.get(f'/api/resident/{residente.id}/audio', headers=worker_headers)

    assert res.status_code == 503
    assert 'configurado' in res.get_json()['error']


def test_si_el_servicio_de_voz_falla_no_se_guarda_nada(
        client, residente, worker_headers, con_clave, app):
    with patch('app.utils._tts_mp3', return_value=(None, 'No se ha podido generar el audio')):
        res = client.get(f'/api/resident/{residente.id}/audio', headers=worker_headers)

    assert res.status_code == 502
    carpeta = os.path.join(app.config['UPLOAD_FOLDER'], 'resident_audio')
    assert not os.path.isdir(carpeta) or os.listdir(carpeta) == []


def test_el_audio_del_residente_exige_token(client, residente, voz):
    assert client.get(f'/api/resident/{residente.id}/audio').status_code == 401


# ── El audio de las instrucciones ────────────────────────────────────────────

def test_devuelve_el_mp3_de_las_instrucciones(client, db, worker_headers, voz):
    ct = _tipo(db, 'Levantar', *TODO_EL_DIA,
               instrucciones='Subir la persiana, asear y vestir.')

    res = client.get(f'/api/care-type/{ct.id}/audio', headers=worker_headers)

    assert res.status_code == 200
    assert res.data == MP3
    assert 'persiana' in voz.call_args[0][0]


def test_un_tipo_sin_instrucciones_da_404(client, db, worker_headers, voz):
    ct = _tipo(db, 'Otro')

    assert client.get(f'/api/care-type/{ct.id}/audio',
                      headers=worker_headers).status_code == 404
    assert voz.call_count == 0


def test_el_audio_de_instrucciones_exige_token(client, db, voz):
    ct = _tipo(db, 'Levantar', instrucciones='Asear.')

    assert client.get(f'/api/care-type/{ct.id}/audio').status_code == 401


# ── Lo que llega al escanear ─────────────────────────────────────────────────

def _escanear(client, headers, residente, cleaner_user):
    return client.post('/api/nfc/scan', headers=headers,
                       json={'nfc_code': residente.nfc_code, 'mode': 'care',
                             'worker_id': cleaner_user.id}).get_json()


def test_al_escanear_llega_la_informacion_del_residente(
        client, residente, cleaner_user, worker_headers, con_clave):
    datos = _escanear(client, worker_headers, residente, cleaner_user)

    assert datos['action'] == 'started'
    assert 'sorda' in datos['resident_info']['text']
    assert datos['resident_info']['audio'] is True


def test_sin_clave_de_openai_la_webapp_no_ofrece_el_boton(
        client, db, residente, cleaner_user, worker_headers):
    """Un botón que siempre falla es peor que no tener botón."""
    _tipo(db, 'Levantar', *TODO_EL_DIA, instrucciones='Asear y vestir.')

    datos = _escanear(client, worker_headers, residente, cleaner_user)

    assert datos['resident_info']['audio'] is False
    assert datos['care_hint']['types'][0]['audio'] is False


def test_cada_tipo_llega_por_separado_con_sus_instrucciones(
        client, db, residente, cleaner_user, worker_headers, con_clave):
    """Con dos atenciones a la misma hora, cada una necesita su botón."""
    _tipo(db, 'Levantar', *TODO_EL_DIA, instrucciones='Asear y vestir.')
    _tipo(db, 'Medicación', *TODO_EL_DIA, instrucciones='Dar las pastillas.')

    tipos = _escanear(client, worker_headers, residente,
                      cleaner_user)['care_hint']['types']

    assert [t['name'] for t in tipos] == ['Levantar', 'Medicación']
    assert all(t['audio'] for t in tipos)
    assert tipos[0]['instructions'] == 'Asear y vestir.'


def test_un_tipo_sin_instrucciones_viene_sin_audio(
        client, db, residente, cleaner_user, worker_headers, con_clave):
    _tipo(db, 'Levantar', *TODO_EL_DIA)

    tipo = _escanear(client, worker_headers, residente,
                     cleaner_user)['care_hint']['types'][0]

    assert tipo['instructions'] is None
    assert tipo['audio'] is False


def test_la_informacion_llega_aunque_no_haya_ningun_tipo_en_horario(
        client, residente, cleaner_user, worker_headers, con_clave):
    """La información del residente vale igual sin horarios configurados."""
    datos = _escanear(client, worker_headers, residente, cleaner_user)

    assert 'care_hint' not in datos
    assert 'sorda' in datos['resident_info']['text']


def test_sin_informacion_y_sin_horarios_no_se_manda_nada(
        client, db, cleaner_user, worker_headers, con_clave):
    """Regresión: quien no tenga nada que contar no gana pantallas de más."""
    r = Resident(name='Sin datos', nfc_code='RES-AUD-3', active=True)
    db.session.add(r)
    db.session.commit()

    datos = _escanear(client, worker_headers, r, cleaner_user)

    assert 'care_hint' not in datos
    assert 'resident_info' not in datos


def test_al_finalizar_no_se_manda_la_informacion_del_residente(
        client, db, residente, cleaner_user, worker_headers, con_clave):
    """Se lee antes de empezar; al cerrar solo estorbaría."""
    rec = CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                     start_time=datetime.now() - timedelta(minutes=5))
    db.session.add(rec)
    db.session.commit()

    datos = _escanear(client, worker_headers, residente, cleaner_user)

    assert datos['action'] == 'select_care_type_end'
    assert 'resident_info' not in datos
