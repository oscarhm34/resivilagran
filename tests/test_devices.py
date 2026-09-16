"""
test_devices.py — Tests del registro de moviles de las trabajadoras.

Endpoints cubiertos:
- POST /login                   → da de alta el movil desde el que se entra
- GET  /devices                 → listado de moviles (admin)
- POST /devices/<id>/rename     → poner nombre a un movil
- POST /devices/<id>/delete     → eliminar un movil

Casos cubiertos:
- El parser de User-Agent, caso por caso, sin pasar por HTTP
- Login sin cabecera: se comporta exactamente como antes
- Login con cabecera: alta del movil y de su fila de uso
- Repetir login: no duplica, cuenta el inicio de sesion
- Dos trabajadoras en el mismo movil: un movil, dos usuarias
- Identificadores invalidos: se ignoran sin tumbar el login
- El movil tambien se registra sin pasar por el formulario de login, que es
  lo que hace la webapp cuando ya tiene el token guardado
- Autorizacion del panel, renombrar y eliminar
"""

import json
import pytest

from app import db as _db
from app.models import (Cleaner, WorkerDevice, WorkerDeviceUse, WorkerDeviceLogin,
                        CleaningRecord, CareRecord)
from app.utils import _describir_dispositivo, _device_uid_valido, desde_hace


UID = 'a1b2c3d4-e5f6-4788-9a0b-1c2d3e4f5061'
UID_2 = 'ffffffff-1111-4222-8333-444455556666'

UA_SAMSUNG = ('Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')
# Lo que manda Chrome desde la version 110: el modelo ya no viaja en el
# User-Agent, todos los Android dicen "K".
UA_RECORTADO = ('Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36')


def login(client, username='limpiadora1', password='limpia123', headers=None):
    return client.post('/login',
                       data=json.dumps({'username': username, 'password': password}),
                       content_type='application/json',
                       headers=headers or {})


def cabeceras(uid=UID, ua=UA_SAMSUNG, modelo=None):
    h = {'X-Device-Id': uid, 'User-Agent': ua}
    if modelo:
        h['X-Device-Model'] = modelo
    return h


# ── El parser, sin pasar por HTTP ────────────────────────────────────────────

class TestDescribirDispositivo:
    """Interpretacion del User-Agent. Es donde estan los casos raros."""

    def test_android_con_modelo_en_el_user_agent(self):
        modelo, sistema, navegador = _describir_dispositivo(UA_SAMSUNG)
        assert modelo == 'Samsung Galaxy A54'
        assert sistema == 'Android 14'
        assert navegador == 'Chrome 120'

    def test_chrome_moderno_no_da_el_modelo(self):
        """Desde Chrome 110 el User-Agent dice "K" en vez del modelo."""
        modelo, sistema, navegador = _describir_dispositivo(UA_RECORTADO)
        assert modelo is None            # "K" no es un telefono
        assert sistema == 'Android 10'
        assert navegador == 'Chrome 131'

    def test_el_modelo_del_cliente_manda_sobre_el_user_agent(self):
        """Client Hints es la unica via para el modelo en Chrome moderno."""
        modelo, _, _ = _describir_dispositivo(UA_RECORTADO, 'SM-A546B')
        assert modelo == 'Samsung Galaxy A54'

    def test_modelo_desconocido_se_ensena_con_la_marca(self):
        modelo, _, _ = _describir_dispositivo(UA_RECORTADO, '23078RKD5G')
        assert modelo == 'Xiaomi 23078RKD5G'

    def test_iphone_no_da_el_modelo_pero_si_el_sistema(self):
        ua = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) '
              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 '
              'Mobile/15E148 Safari/604.1')
        modelo, sistema, navegador = _describir_dispositivo(ua)
        assert modelo == 'iPhone'
        assert sistema == 'iOS 17.4'
        assert navegador == 'Safari 17'

    def test_edge_no_se_confunde_con_chrome(self):
        ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0')
        modelo, sistema, navegador = _describir_dispositivo(ua)
        assert modelo == 'Ordenador'
        assert sistema == 'Windows 10 u 11'
        assert navegador == 'Edge 127'

    def test_el_idioma_no_se_confunde_con_un_modelo(self):
        ua = ('Mozilla/5.0 (Linux; Android 11; es-es; SM-A136B) '
              'AppleWebKit/537.36 Chrome/110.0 Mobile Safari/537.36')
        modelo, _, _ = _describir_dispositivo(ua)
        assert modelo != 'es-es'

    @pytest.mark.parametrize('ua', ['', None, 'basura-sin-sentido'])
    def test_lo_que_no_reconoce_no_se_lo_inventa(self, ua):
        assert _describir_dispositivo(ua) == (None, None, None)


class TestDeviceUidValido:

    @pytest.mark.parametrize('uid', [UID, 'f-abc123def456', 'A' * 64])
    def test_acepta_identificadores_con_forma(self, uid):
        assert _device_uid_valido(uid) == uid

    @pytest.mark.parametrize('uid', [
        None, '', '   ', 'corto', "' OR 1=1 --", 'con espacio', 'A' * 65,
        '../../etc/passwd', '<script>alert(1)</script>',
    ])
    def test_rechaza_lo_que_no_tiene_forma(self, uid):
        assert _device_uid_valido(uid) is None


# ── El registro en el login ──────────────────────────────────────────────────

class TestLoginRegistraDispositivo:

    def test_sin_cabecera_el_login_funciona_igual_que_antes(self, client, cleaner_user):
        """Compatibilidad: la app Android antigua no manda la cabecera."""
        response = login(client)
        assert response.status_code == 200
        assert response.get_json()['access_token']
        assert WorkerDevice.query.count() == 0

    def test_con_cabecera_da_de_alta_el_movil_sin_nombre(self, client, cleaner_user):
        response = login(client, headers=cabeceras())
        assert response.status_code == 200

        device = WorkerDevice.query.one()
        assert device.device_uid == UID
        assert device.label is None                  # lo pone coordinacion
        assert device.model == 'Samsung Galaxy A54'
        assert device.os_name == 'Android 14'
        assert device.browser == 'Chrome 120'
        assert device.user_agent == UA_SAMSUNG

        uso = WorkerDeviceUse.query.one()
        assert uso.worker_id == cleaner_user.id
        assert uso.device_id == device.id
        assert uso.login_count == 1

    def test_el_modelo_llega_por_client_hints(self, client, cleaner_user):
        """Con Chrome moderno, sin la cabecera no habria modelo."""
        login(client, headers=cabeceras(ua=UA_RECORTADO, modelo='SM-A556B'))
        assert WorkerDevice.query.one().model == 'Samsung Galaxy A55'

    def test_repetir_login_no_duplica_y_cuenta_la_entrada(self, client, cleaner_user):
        login(client, headers=cabeceras())
        login(client, headers=cabeceras())

        assert WorkerDevice.query.count() == 1
        assert WorkerDeviceUse.query.one().login_count == 2

    def test_dos_trabajadoras_en_el_mismo_movil(self, client, db, cleaner_user):
        """Un movil compartido es UN dispositivo con DOS usuarias."""
        otra = Cleaner(username='limpiadora2', name='Ana Ruiz', is_admin=False)
        otra.set_password('limpia456')
        db.session.add(otra)
        db.session.commit()

        login(client, headers=cabeceras())
        login(client, username='limpiadora2', password='limpia456',
              headers=cabeceras())

        assert WorkerDevice.query.count() == 1
        usos = WorkerDeviceUse.query.all()
        assert len(usos) == 2
        assert {u.worker_id for u in usos} == {cleaner_user.id, otra.id}

    def test_dos_moviles_iguales_son_dos_dispositivos(self, client, cleaner_user):
        """Mismo modelo, mismo User-Agent: solo el identificador los separa."""
        login(client, headers=cabeceras(uid=UID))
        login(client, headers=cabeceras(uid=UID_2))

        assert WorkerDevice.query.count() == 2
        assert WorkerDeviceUse.query.count() == 2

    @pytest.mark.parametrize('uid', ["' OR 1=1 --", 'x' * 200, 'ab', ''])
    def test_identificador_invalido_se_ignora_sin_tumbar_el_login(
            self, client, cleaner_user, uid):
        response = login(client, headers={'X-Device-Id': uid,
                                          'User-Agent': UA_SAMSUNG})
        assert response.status_code == 200
        assert response.get_json()['access_token']
        assert WorkerDevice.query.count() == 0

    def test_cuenta_desactivada_no_registra_movil(self, client, db, cleaner_user):
        cleaner_user.active = False
        db.session.commit()

        response = login(client, headers=cabeceras())
        assert response.status_code == 403
        assert WorkerDevice.query.count() == 0


class TestRegistroSinPasarPorElLogin:
    """La webapp entra sola si encuentra el token guardado.

    Es el caso importante: una trabajadora puede pasarse semanas sin volver a
    ver el formulario de login, asi que si el movil solo se anotara ahi, el
    dato se quedaria congelado. El registro va tambien en el `after_request`.
    """

    def test_una_peticion_cualquiera_registra_el_movil(self, client, cleaner_user):
        token = login(client).get_json()['access_token']   # login SIN cabecera
        assert WorkerDevice.query.count() == 0

        client.get('/check_cleaning',
                   headers={'Authorization': 'Bearer ' + token,
                            'X-Device-Id': UID,
                            'User-Agent': UA_SAMSUNG})

        device = WorkerDevice.query.one()
        assert device.device_uid == UID
        assert device.model == 'Samsung Galaxy A54'
        # No es un inicio de sesion: el contador no se toca.
        assert WorkerDeviceUse.query.one().login_count == 1


# ── Panel de administracion ──────────────────────────────────────────────────

@pytest.fixture
def device(db, cleaner_user):
    """Un movil ya dado de alta, con su fila de uso."""
    from datetime import datetime
    d = WorkerDevice(device_uid=UID, user_agent=UA_SAMSUNG,
                     model='Samsung Galaxy A54', os_name='Android 14',
                     browser='Chrome 120',
                     first_seen=datetime.now(), last_seen=datetime.now())
    db.session.add(d)
    db.session.flush()
    db.session.add(WorkerDeviceUse(device_id=d.id, worker_id=cleaner_user.id,
                                   last_used=datetime.now(), login_count=3))
    db.session.commit()
    return d


class TestMovilesCompartidos:
    """Los telefonos no son de nadie: cada una coge el que esta libre.

    Es el caso real de la residencia, y el que rompe cualquier diseno que
    suponga un movil por persona.
    """

    def test_quien_lo_lleva_es_la_ultima_que_lo_toco(self, client, db, cleaner_user):
        otra = Cleaner(username='limpiadora2', name='Ana Ruiz', is_admin=False)
        otra.set_password('limpia456')
        db.session.add(otra)
        db.session.commit()

        login(client, headers=cabeceras())                      # Maria primero
        login(client, username='limpiadora2', password='limpia456',
              headers=cabeceras())                              # Ana despues

        device = WorkerDevice.query.one()
        assert device.quien_lo_lleva().worker_id == otra.id
        assert device.en_uso is True

    def test_una_trabajadora_puede_cambiar_de_movil(self, client, cleaner_user):
        """Coge el 1 por la manana y el 2 por la tarde: dos dispositivos, y su
        ultimo movil es el segundo."""
        login(client, headers=cabeceras(uid=UID))
        login(client, headers=cabeceras(uid=UID_2))

        assert WorkerDevice.query.count() == 2
        from app.utils import _ultimo_movil_por_trabajadora
        ultimo = _ultimo_movil_por_trabajadora()[cleaner_user.id]
        assert ultimo.device.device_uid == UID_2

    def test_un_movil_parado_no_lo_lleva_nadie(self, db, cleaner_user):
        from datetime import datetime, timedelta
        hace_rato = datetime.now() - timedelta(hours=3)
        d = WorkerDevice(device_uid=UID, first_seen=hace_rato, last_seen=hace_rato)
        db.session.add(d)
        db.session.flush()
        db.session.add(WorkerDeviceUse(device_id=d.id, worker_id=cleaner_user.id,
                                       last_used=hace_rato, login_count=1))
        db.session.commit()

        assert d.en_uso is False
        assert d.quien_lo_lleva().worker_id == cleaner_user.id   # la ultima que lo uso

    def test_dias_parado_localiza_los_perdidos(self, db):
        from datetime import datetime, timedelta
        viejo = datetime.now() - timedelta(days=12)
        d = WorkerDevice(device_uid=UID, first_seen=viejo, last_seen=viejo)
        db.session.add(d)
        db.session.commit()
        assert d.dias_parado == 12


class TestHistorial:

    def test_cada_login_deja_una_entrada(self, client, cleaner_user):
        login(client, headers=cabeceras())
        login(client, headers=cabeceras())
        assert WorkerDeviceLogin.query.count() == 2

    def test_navegar_no_ensucia_el_historial(self, client, cleaner_user):
        """Solo los inicios de sesion, no cada peticion: si no, el historial
        seria ilegible y crecería sin freno."""
        token = login(client, headers=cabeceras()).get_json()['access_token']
        for _ in range(3):
            client.get('/check_cleaning',
                       headers={'Authorization': 'Bearer ' + token,
                                'X-Device-Id': UID, 'User-Agent': UA_SAMSUNG})
        assert WorkerDeviceLogin.query.count() == 1

    def test_la_pagina_de_historial_filtra(self, auth_client, client, db, cleaner_user):
        login(client, headers=cabeceras())
        response = auth_client.get('/devices/historial')
        assert response.status_code == 200
        assert 'Maria García' in response.get_data(as_text=True)

        device = WorkerDevice.query.one()
        assert auth_client.get('/devices/historial?device_id=%d' % device.id)\
            .status_code == 200
        # Filtrando por otro movil no debe salir esta entrada
        vacio = auth_client.get('/devices/historial?device_id=99999')
        assert 'No hay ninguna entrada' in vacio.get_data(as_text=True)

    def test_el_periodo_se_acota_a_un_rango_sensato(self, auth_client, db):
        for dias in ('0', '-5', '99999', 'abc'):
            assert auth_client.get('/devices/historial?dias=%s' % dias).status_code == 200


class TestSelloEnLosRegistros:
    """Con los moviles rotando, saber la persona ya no dice el aparato."""

    def test_una_limpieza_guarda_desde_que_movil_se_hizo(self, client, db,
                                                         cleaner_user, room):
        token = login(client, headers=cabeceras()).get_json()['access_token']
        device = WorkerDevice.query.one()

        response = client.post('/start_cleaning',
                               data=json.dumps({'room_id': room.number,
                                                'cleaner_id': cleaner_user.id}),
                               content_type='application/json',
                               headers={'Authorization': 'Bearer ' + token,
                                        'X-Device-Id': UID, 'User-Agent': UA_SAMSUNG})
        assert response.status_code in (200, 201), response.get_data(as_text=True)
        assert CleaningRecord.query.one().device_id == device.id

    def test_sin_cabecera_el_registro_se_guarda_igual(self, client, db,
                                                      cleaner_user, room):
        """La app Android antigua no manda nada: no puede impedir trabajar."""
        token = login(client).get_json()['access_token']
        response = client.post('/start_cleaning',
                               data=json.dumps({'room_id': room.number,
                                                'cleaner_id': cleaner_user.id}),
                               content_type='application/json',
                               headers={'Authorization': 'Bearer ' + token})
        assert response.status_code in (200, 201)
        assert CleaningRecord.query.one().device_id is None

    def test_borrar_el_movil_no_borra_los_registros(self, auth_client, client, db,
                                                   cleaner_user, room):
        token = login(client, headers=cabeceras()).get_json()['access_token']
        client.post('/start_cleaning',
                    data=json.dumps({'room_id': room.number,
                                     'cleaner_id': cleaner_user.id}),
                    content_type='application/json',
                    headers={'Authorization': 'Bearer ' + token,
                             'X-Device-Id': UID, 'User-Agent': UA_SAMSUNG})
        device = WorkerDevice.query.one()

        auth_client.post('/devices/%d/delete' % device.id, follow_redirects=True)

        registro = CleaningRecord.query.one()
        assert registro is not None              # la limpieza sigue ahi
        assert registro.device_id is None        # solo pierde el sello


class TestDesdeHace:

    def test_lo_de_ahora_mismo(self):
        from datetime import datetime
        assert desde_hace(datetime.now()) == 'ahora mismo'

    def test_minutos_horas_y_dias(self):
        from datetime import datetime, timedelta
        ahora = datetime.now()
        assert desde_hace(ahora - timedelta(minutes=5)) == 'hace 5 min'
        assert desde_hace(ahora - timedelta(hours=3)) == 'hace 3 horas'
        assert desde_hace(ahora - timedelta(days=1, hours=1)) == 'ayer'
        assert desde_hace(ahora - timedelta(days=3)) == 'hace 3 dias'
        assert desde_hace(ahora - timedelta(days=40)).startswith('el ')

    def test_sin_fecha(self):
        assert desde_hace(None) == 'nunca'


class TestPanelDispositivos:

    def test_sin_admin_redirige_al_login(self, client, db):
        response = client.get('/devices')
        assert response.status_code == 302
        assert '/admin/login' in response.headers['Location']

    def test_el_listado_ensena_el_movil_y_quien_lo_lleva(self, auth_client, device,
                                                         cleaner_user):
        response = auth_client.get('/devices')
        assert response.status_code == 200
        texto = response.get_data(as_text=True)
        assert 'Samsung Galaxy A54' in texto
        assert 'Maria García' in texto
        assert 'Sin nombre' in texto

    def test_el_listado_vacio_no_revienta(self, auth_client, db):
        response = auth_client.get('/devices')
        assert response.status_code == 200
        assert 'Todavía no ha entrado nadie' in response.get_data(as_text=True)

    def test_renombrar_guarda_el_nombre(self, auth_client, device):
        response = auth_client.post('/devices/%d/rename' % device.id,
                                    data={'label': 'Móvil planta 1'},
                                    follow_redirects=True)
        assert response.status_code == 200
        assert _db.session.get(WorkerDevice, device.id).label == 'Móvil planta 1'

    def test_nombre_vacio_lo_deja_sin_nombre(self, auth_client, device):
        auth_client.post('/devices/%d/rename' % device.id, data={'label': 'Uno'})
        auth_client.post('/devices/%d/rename' % device.id, data={'label': '   '})
        assert _db.session.get(WorkerDevice, device.id).label is None

    def test_eliminar_se_lleva_tambien_las_filas_de_uso(self, auth_client, device):
        device_id = device.id
        response = auth_client.post('/devices/%d/delete' % device_id,
                                    follow_redirects=True)
        assert response.status_code == 200
        assert _db.session.get(WorkerDevice, device_id) is None
        assert WorkerDeviceUse.query.count() == 0

    def test_renombrar_un_movil_que_no_existe_da_404(self, auth_client, db):
        assert auth_client.post('/devices/9999/rename',
                                data={'label': 'X'}).status_code == 404


class TestColumnaMovilEnEmpleados:

    def test_el_movil_sale_en_la_tabla_de_empleados(self, auth_client, device,
                                                    cleaner_user):
        response = auth_client.get('/manage_workers')
        assert response.status_code == 200
        texto = response.get_data(as_text=True)
        assert 'data-sort="device"' in texto
        assert 'Samsung Galaxy A54' in texto

    def test_quien_no_ha_entrado_nunca_no_rompe_la_tabla(self, auth_client,
                                                         cleaner_user):
        response = auth_client.get('/manage_workers')
        assert response.status_code == 200
        assert 'data-device=""' in response.get_data(as_text=True)
