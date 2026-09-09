"""
test_push_entrega.py — Que el aviso llegue de verdad al movil.

Los avisos push se han "arreglado" tres veces y las trabajadoras seguian
diciendo que el movil no les suena. Lo que se fija aqui son los fallos que hacen
que un envio se pierda sin dejar rastro, que son los peores de diagnosticar:
todo responde 200, el log no dice nada y el telefono no vibra.

El bloqueo principal —el certificado del NAS, que impide a Chrome registrar el
service worker— es de infraestructura y no se puede cubrir con un test. Lo que
si se cubre es todo lo que fallaria igualmente con el certificado bien.
"""

import pytest
from flask_jwt_extended import create_access_token

from app.models import Cleaner, PushSubscription
import app.blueprints.notifications as notif


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def suscrita(db, cleaner_user):
    s = PushSubscription(worker_id=cleaner_user.id,
                         endpoint='https://fcm.googleapis.com/fcm/send/abc',
                         keys_json='{"p256dh": "clave", "auth": "secreto"}')
    db.session.add(s)
    db.session.commit()
    return s


class _RespuestaFalsa:
    def __init__(self, codigo):
        self.status_code = codigo


def _webpush_falso(monkeypatch, efecto=None):
    """Sustituye pywebpush y recoge con que argumentos se le llama.

    pywebpush no esta instalado en el entorno local —solo en la imagen de
    Docker—, asi que se inyecta un modulo falso en sys.modules.
    """
    import sys
    import types

    llamadas = []

    class WebPushException(Exception):
        def __init__(self, mensaje, response=None):
            super().__init__(mensaje)
            self.response = response

    def webpush(**kwargs):
        llamadas.append(kwargs)
        if efecto:
            raise efecto(WebPushException)

    modulo = types.ModuleType('pywebpush')
    modulo.webpush = webpush
    modulo.WebPushException = WebPushException
    monkeypatch.setitem(sys.modules, 'pywebpush', modulo)
    return llamadas


# ── El fallo que hacia que "a veces llegue y a veces no" ─────────────────────

def test_el_aviso_se_guarda_si_el_movil_esta_dormido(db, app, suscrita,
                                                     cleaner_user, monkeypatch):
    """Sin ttl, pywebpush manda cero: entregalo ahora o tiralo.

    Un movil en el bolsillo o en reposo perdia el aviso para siempre, y el
    servicio push respondia como si hubiera ido bien.
    """
    llamadas = _webpush_falso(monkeypatch)

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Hola', 'Mensaje')

    assert len(llamadas) == 1
    assert llamadas[0]['ttl'] >= 3600, 'el aviso tiene que sobrevivir a un turno'


def test_el_aviso_pide_despertar_al_movil(db, app, suscrita, cleaner_user,
                                          monkeypatch):
    llamadas = _webpush_falso(monkeypatch)

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Hola', 'Mensaje')

    assert llamadas[0]['headers']['Urgency'] == 'high'


def test_el_envio_no_se_queda_colgado_para_siempre(db, app, suscrita,
                                                   cleaner_user, monkeypatch):
    """pywebpush no pone timeout: una conexion muerta bloquea un hilo sin fin."""
    llamadas = _webpush_falso(monkeypatch)

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Hola', 'Mensaje')

    assert 0 < llamadas[0]['timeout'] <= 60


# ── Suscripciones muertas ────────────────────────────────────────────────────

@pytest.mark.parametrize('codigo', [403, 404, 410])
def test_una_suscripcion_muerta_se_borra(db, app, suscrita, cleaner_user,
                                         monkeypatch, codigo):
    """El 403 es el importante: es lo que devuelve el servicio push si cambian
    las claves VAPID, y antes no se detectaba. Esas suscripciones fallaban en
    cada envio para siempre mientras el estado seguia diciendo que habia
    dispositivos registrados."""
    _webpush_falso(monkeypatch,
                   efecto=lambda exc: exc('rechazado', _RespuestaFalsa(codigo)))

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Hola', 'Mensaje')

    assert PushSubscription.query.count() == 0


def test_un_error_pasajero_no_borra_la_suscripcion(db, app, suscrita,
                                                   cleaner_user, monkeypatch):
    """Un 500 del servicio push es cosa suya: el movil sigue siendo bueno."""
    _webpush_falso(monkeypatch,
                   efecto=lambda exc: exc('error del servidor', _RespuestaFalsa(500)))

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Hola', 'Mensaje')

    assert PushSubscription.query.count() == 1


def test_el_numero_del_error_no_se_confunde_con_el_codigo(db, app, suscrita,
                                                          cleaner_user, monkeypatch):
    """Antes se buscaba '410' dentro del texto del error, y eso acierta tambien
    con un numero de bytes o un identificador de peticion."""
    _webpush_falso(monkeypatch,
                   efecto=lambda exc: exc('timeout tras 410 ms', _RespuestaFalsa(500)))

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Hola', 'Mensaje')

    assert PushSubscription.query.count() == 1


# ── La prueba de aviso tiene que decir la verdad ─────────────────────────────

def test_la_prueba_dice_cuantos_han_llegado(client, db, suscrita,
                                            worker_headers, monkeypatch):
    _webpush_falso(monkeypatch)

    datos = client.post('/api/push/test', headers=worker_headers).get_json()

    assert datos['ok'] is True
    assert datos['entregados'] == 1
    assert datos['fallidos'] == 0


def test_la_prueba_no_dice_que_si_cuando_ha_fallado(client, db, suscrita,
                                                    worker_headers, monkeypatch):
    """Devolvia 200 y el numero de moviles registrados nada mas lanzar el hilo,
    o sea que decia que si antes de saberlo. Era el unico diagnostico que una
    trabajadora podia hacerse sola."""
    _webpush_falso(monkeypatch,
                   efecto=lambda exc: exc('rechazado', _RespuestaFalsa(500)))

    datos = client.post('/api/push/test', headers=worker_headers).get_json()

    assert datos['ok'] is False
    assert datos['entregados'] == 0
    assert datos['fallidos'] == 1


def test_la_prueba_no_filtra_el_detalle_del_error_al_movil(client, db, suscrita,
                                                           worker_headers, monkeypatch):
    """El motivo tecnico va al log del servidor: a quien esta limpiando una
    habitacion no le dice nada y puede nombrar rutas internas."""
    _webpush_falso(monkeypatch,
                   efecto=lambda exc: exc('https://fcm.../interno rechazado',
                                          _RespuestaFalsa(500)))

    datos = client.post('/api/push/test', headers=worker_headers).get_json()

    assert 'fcm' not in str(datos).lower()


def test_sin_moviles_registrados_la_prueba_lo_distingue(client, db,
                                                        worker_headers, monkeypatch):
    _webpush_falso(monkeypatch)

    datos = client.post('/api/push/test', headers=worker_headers).get_json()

    assert datos['dispositivos'] == 0
    assert datos['entregados'] == 0


def test_la_prueba_sigue_pidiendo_sesion(client, db, suscrita):
    assert client.post('/api/push/test').status_code == 401


# ── Que los avisos no se pisen ───────────────────────────────────────────────

def test_cada_aviso_lleva_su_propia_etiqueta(db, app, suscrita, cleaner_user,
                                             monkeypatch):
    """Con la misma etiqueta para todos, el ultimo aviso borra al anterior: con
    una sesion abierta y un traspaso pendientes solo se veia uno."""
    import json
    from app.models import Notification

    llamadas = _webpush_falso(monkeypatch)
    avisos = []
    for i in range(2):
        n = Notification(worker_id=cleaner_user.id, title=f'Aviso {i}',
                         message='x', type='info')
        db.session.add(n)
        avisos.append(n)
    db.session.commit()

    with app.test_request_context():
        for n in avisos:
            notif.send_push_for_notification(n)

    etiquetas = [json.loads(c['data'])['tag'] for c in llamadas]
    assert len(set(etiquetas)) == 2, etiquetas


# ── El manifest, que Android necesita para dar la app por instalable ────────

def test_el_manifest_trae_el_icono_grande(client):
    iconos = client.get('/worker/manifest.json').get_json()['icons']

    tamanos = {i['sizes'] for i in iconos}
    assert '512x512' in tamanos
    assert '192x192' in tamanos


def test_el_manifest_declara_el_ambito(client):
    """Del ambito depende que el service worker controle la pagina, y de eso
    dependen los avisos."""
    assert client.get('/worker/manifest.json').get_json()['scope'] == '/'


# ── Tono del aviso ──────────────────────────────────────────────────────────
#
# El sonido de un aviso con la aplicacion cerrada lo decide Android y la web no
# puede cambiarlo. La vibracion si viaja en el aviso, asi que es lo unico que
# distingue un tono de otro con el movil en el bolsillo: si el patron no sale de
# aqui, elegir el tono no sirve de nada en segundo plano.

def test_el_aviso_lleva_la_vibracion_del_tono_elegido(db, app, suscrita,
                                                      cleaner_user, monkeypatch):
    import json
    from app.utils import TONOS_AVISO

    cleaner_user.msg_tone = 'triple'
    db.session.commit()
    llamadas = _webpush_falso(monkeypatch)

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Hola', 'Mensaje')

    assert json.loads(llamadas[0]['data'])['vibrate'] == TONOS_AVISO['triple']


def test_sin_tono_elegido_el_aviso_vibra_como_siempre(db, app, suscrita,
                                                      cleaner_user, monkeypatch):
    """NULL en la columna no puede dejar el aviso sin vibracion: quien no ha
    elegido nada tiene que seguir notando el movil igual que antes."""
    import json
    from app.utils import TONOS_AVISO, TONO_AVISO_DEFECTO

    assert cleaner_user.msg_tone is None
    llamadas = _webpush_falso(monkeypatch)

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Hola', 'Mensaje')

    assert json.loads(llamadas[0]['data'])['vibrate'] == TONOS_AVISO[TONO_AVISO_DEFECTO]


def test_guardar_el_tono_exige_token(client):
    assert client.put('/api/worker/msg-tone', json={'tone': 'campana'}).status_code == 401


def test_guardar_el_tono_lo_deja_en_el_perfil(client, db, cleaner_user, worker_headers):
    r = client.put('/api/worker/msg-tone', json={'tone': 'campana'},
                   headers=worker_headers)

    assert r.status_code == 200
    assert r.get_json()['tone'] == 'campana'
    assert db.session.get(Cleaner, cleaner_user.id).msg_tone == 'campana'


def test_un_tono_inventado_se_rechaza(client, db, cleaner_user, worker_headers):
    r = client.put('/api/worker/msg-tone', json={'tone': 'sirena'},
                   headers=worker_headers)

    assert r.status_code == 400
    assert db.session.get(Cleaner, cleaner_user.id).msg_tone is None


def test_el_tono_se_guarda_en_quien_manda_el_token(client, db, cleaner_user,
                                                   worker_headers):
    """La identidad sale del token: un id en el cuerpo no puede cambiarle el
    tono a otra trabajadora."""
    otra = Cleaner(username='otra', name='Otra', is_admin=False)
    otra.set_password('x')
    db.session.add(otra)
    db.session.commit()

    client.put('/api/worker/msg-tone',
               json={'tone': 'grave', 'worker_id': otra.id, 'cleaner_id': otra.id},
               headers=worker_headers)

    assert db.session.get(Cleaner, cleaner_user.id).msg_tone == 'grave'
    assert db.session.get(Cleaner, otra.id).msg_tone is None


def test_la_configuracion_devuelve_el_tono(client, db, cleaner_user, worker_headers):
    """La webapp lo lee de aqui para que el tono elegido en otro movil llegue a
    este sin volver a elegirlo."""
    cleaner_user.msg_tone = 'grave'
    db.session.commit()

    assert client.get('/api/config', headers=worker_headers).get_json()['msg_tone'] == 'grave'


# ── Aviso de prueba desde el panel ──────────────────────────────────────────

def test_el_aviso_de_prueba_del_panel_exige_admin(client, cleaner_user):
    r = client.post(f'/cleaners/{cleaner_user.id}/push-test')

    assert r.status_code in (302, 401, 403)


def test_el_aviso_de_prueba_del_panel_cuenta_los_entregados(
        auth_client, db, app, cleaner_user, suscrita, monkeypatch):
    """Coordinacion no puede comprobar nada con el movil de otra persona
    delante. El envio es sincrono y dice lo que ha pasado de verdad."""
    _webpush_falso(monkeypatch)

    datos = auth_client.post(f'/cleaners/{cleaner_user.id}/push-test').get_json()

    assert datos == {'ok': True, 'dispositivos': 1, 'entregados': 1, 'fallidos': 0}


def test_el_aviso_de_prueba_avisa_si_no_hay_movil_registrado(
        auth_client, db, cleaner_user, monkeypatch):
    _webpush_falso(monkeypatch)

    datos = auth_client.post(f'/cleaners/{cleaner_user.id}/push-test').get_json()

    assert datos['dispositivos'] == 0
    assert datos['ok'] is False


def test_la_prueba_se_ve_aunque_la_aplicacion_este_abierta(db, app, suscrita,
                                                           cleaner_user, monkeypatch):
    """El service worker esconde el aviso de un mensaje cuando la pantalla ya
    esta delante, para no avisar dos veces. La prueba no: sirve para comprobar
    que la notificacion del sistema sale, y escondida no comprobaria nada."""
    import json
    llamadas = _webpush_falso(monkeypatch)

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Aviso de prueba', 'x',
                                  tag='prueba', siempre_visible=True)

    assert json.loads(llamadas[0]['data'])['siempre_visible'] is True


def test_el_aviso_de_un_mensaje_no_se_marca_como_siempre_visible(
        db, app, suscrita, cleaner_user, monkeypatch):
    import json
    llamadas = _webpush_falso(monkeypatch)

    with app.test_request_context():
        notif.send_push_to_worker(cleaner_user.id, 'Ana', 'Te ha enviado un mensaje',
                                  tag='chat-1')

    assert json.loads(llamadas[0]['data'])['siempre_visible'] is False
