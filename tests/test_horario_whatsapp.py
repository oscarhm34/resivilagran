"""
test_horario_whatsapp.py — Mandar el horario por WhatsApp y recoger el acuse.

Aqui hay dos cosas que no se parecen a nada del resto del proyecto y por eso se
vigilan de cerca:

1. **Una ruta publica.** `/horario/<token>` no lleva `@admin_required` ni
   `@jwt_required()`: lo que autoriza es la firma del enlace. Si alguien le
   pusiera un decorador de sesion sin querer, dejaria de abrirse en el movil de
   la trabajadora, asi que un test fija que sigue siendo publica. Y al reves: el
   token tiene que valer solo para quien lo recibio y para su semana.
2. **Una llamada que cuesta dinero** y sale a un tercero. Se comprueba que no se
   intenta sin configuracion, que un fallo queda escrito con su motivo y que un
   fallo nunca se apunta como enviado.
"""

from datetime import date, datetime, time, timedelta

import pytest

from app.models import (
    Cleaner, ShiftAssignment, ShiftPosition, ShiftType, ShiftWeekReceipt,
)


LUNES = date(2026, 10, 5)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def manana(db):
    st = ShiftType(name='Manana', short_name='M', color='#8ab4e8',
                   start_time=time(7, 30), end_time=time(14, 0),
                   sort_order=1, active=True, board_row='manana')
    db.session.add(st)
    db.session.commit()
    return st


@pytest.fixture
def m1(db, manana):
    p = ShiftPosition(shift_type_id=manana.id, code='M1', sort_order=0)
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def m2(db, manana):
    p = ShiftPosition(shift_type_id=manana.id, code='M2', sort_order=1)
    db.session.add(p)
    db.session.commit()
    return p


def _trabajadora(db, nombre, usuario, telefono=None):
    c = Cleaner(username=usuario, name=nombre, is_admin=False, active=True,
                role='atenciones', phone=telefono)
    c.set_password('x123456')
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture
def ana(db):
    return _trabajadora(db, 'Ana Pons', 'ana', '612345678')


@pytest.fixture
def berta(db):
    return _trabajadora(db, 'Berta Lluch', 'berta', '+34 622 33 44 55')


def _asignar(db, dia, puesto, trabajadora):
    a = ShiftAssignment(date=dia, cleaner_id=trabajadora.id,
                        shift_type_id=puesto.shift_type_id,
                        position_id=puesto.id, is_override=True,
                        created_at=datetime.now())
    db.session.add(a)
    db.session.commit()
    return a


@pytest.fixture
def con_envio(app):
    """La aplicacion como si Meta estuviese dado de alta."""
    previo = {k: app.config.get(k) for k in
              ('WHATSAPP_TOKEN', 'WHATSAPP_PHONE_ID', 'PUBLIC_BASE_URL')}
    app.config.update(WHATSAPP_TOKEN='tok-de-prueba',
                      WHATSAPP_PHONE_ID='111222333',
                      PUBLIC_BASE_URL='https://ejemplo.test:8444')
    yield app
    app.config.update(previo)


class _Respuesta:
    """Lo justo de `requests.Response` para no llamar a Meta de verdad."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b'{}'
        self.text = str(payload)

    def json(self):
        return self._payload


# ── El telefono ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('escrito, esperado', [
    ('612345678', '34612345678'),          # como se escribe aqui
    ('+34 612 34 56 78', '34612345678'),
    ('0034612345678', '34612345678'),
    ('34612345678', '34612345678'),
    ('612 345 678', '34612345678'),
])
def test_el_telefono_sale_en_el_formato_que_pide_whatsapp(app, escrito, esperado):
    from app.blueprints.shifts import _telefono_e164
    assert _telefono_e164(escrito) == esperado


@pytest.mark.parametrize('escrito', ['', None, 'no tiene', '12345', '6'])
def test_un_telefono_que_no_se_entiende_no_se_inventa(app, escrito):
    from app.blueprints.shifts import _telefono_e164
    assert _telefono_e164(escrito) is None


# ── El token del enlace ───────────────────────────────────────────────────────

def test_el_token_devuelve_a_quien_y_de_que_semana_es(app):
    from app.blueprints.shifts import _leer_token_horario, _token_horario
    with app.test_request_context():
        token = _token_horario(7, LUNES)
        assert _leer_token_horario(token) == (7, LUNES)


def test_un_token_tocado_no_vale(app):
    from app.blueprints.shifts import _leer_token_horario, _token_horario
    with app.test_request_context():
        token = _token_horario(7, LUNES)
        # Un caracter cambiado en medio: es lo que pasa cuando se copia mal.
        roto = token[:10] + ('b' if token[10] != 'b' else 'c') + token[11:]
        assert _leer_token_horario(roto) is None
        assert _leer_token_horario('cualquier-cosa') is None


def test_un_token_caducado_no_vale(app, monkeypatch):
    from app.blueprints import shifts
    with app.test_request_context():
        token = shifts._token_horario(7, LUNES)
        monkeypatch.setattr(shifts, '_VALIDEZ_ENLACE', -1)
        assert shifts._leer_token_horario(token) is None


def test_el_token_de_una_semana_no_abre_otra(app):
    from app.blueprints.shifts import _leer_token_horario, _token_horario
    with app.test_request_context():
        otra = LUNES + timedelta(days=7)
        assert _leer_token_horario(_token_horario(7, LUNES))[1] == LUNES
        assert _leer_token_horario(_token_horario(7, otra))[1] == otra


def test_sin_direccion_publica_no_se_puede_construir_el_enlace(app):
    """Sin `ProxyFix`, `url_for(_external=True)` daria el host del contenedor."""
    from app.blueprints.shifts import _enlace_horario
    previo = app.config.get('PUBLIC_BASE_URL')
    app.config['PUBLIC_BASE_URL'] = ''
    try:
        with app.test_request_context():
            assert _enlace_horario(7, LUNES) is None
    finally:
        app.config['PUBLIC_BASE_URL'] = previo


def test_el_enlace_cuelga_de_la_direccion_publica(con_envio):
    from app.blueprints.shifts import _enlace_horario
    with con_envio.test_request_context():
        enlace = _enlace_horario(7, LUNES)
    assert enlace.startswith('https://ejemplo.test:8444/horario/')


# ── La pagina de la trabajadora ───────────────────────────────────────────────

def _token(app, cleaner_id, lunes=LUNES):
    from app.blueprints.shifts import _token_horario
    with app.test_request_context():
        return _token_horario(cleaner_id, lunes)


def test_la_pagina_del_horario_sigue_siendo_publica(client, db, ana, m1, app):
    """Si alguien le pone un decorador de sesion, deja de abrirse en el movil."""
    _asignar(db, LUNES, m1, ana)
    res = client.get('/horario/' + _token(app, ana.id))
    assert res.status_code == 200
    assert 'Ana Pons' in res.get_data(as_text=True)


def test_la_pagina_ensena_sus_turnos_y_no_los_de_otra(client, db, ana, berta,
                                                      m1, m2, app):
    _asignar(db, LUNES, m1, ana)
    _asignar(db, LUNES, m2, berta)

    texto = client.get('/horario/' + _token(app, ana.id)).get_data(as_text=True)
    assert 'Ana Pons' in texto
    assert 'Berta Lluch' not in texto
    assert 'M1' in texto
    assert 'M2' not in texto


def test_la_pagina_escribe_los_siete_dias(client, db, ana, m1, app):
    _asignar(db, LUNES, m1, ana)
    texto = client.get('/horario/' + _token(app, ana.id)).get_data(as_text=True)
    # Un hueco se lee como un olvido; un "Libre" escrito, no.
    assert texto.count('Libre') == 6
    assert '07:30' in texto


def test_un_enlace_roto_da_404_y_no_toca_nada(client, db, ana):
    res = client.get('/horario/esto-no-es-un-token')
    assert res.status_code == 404
    assert 'ya no sirve' in res.get_data(as_text=True)
    assert ShiftWeekReceipt.query.count() == 0


def test_el_enlace_de_una_trabajadora_dada_de_baja_no_abre(client, db, ana, m1, app):
    _asignar(db, LUNES, m1, ana)
    token = _token(app, ana.id)
    ana.active = False
    db.session.commit()
    assert client.get('/horario/' + token).status_code == 404


def test_abrir_la_pagina_apunta_el_visto_una_sola_vez(client, db, ana, m1, app):
    _asignar(db, LUNES, m1, ana)
    token = _token(app, ana.id)

    client.get('/horario/' + token)
    recibo = ShiftWeekReceipt.query.filter_by(cleaner_id=ana.id).one()
    primera = recibo.opened_at
    assert primera is not None
    assert recibo.estado == 'visto'

    client.get('/horario/' + token)
    db.session.expire_all()
    recibo = ShiftWeekReceipt.query.filter_by(cleaner_id=ana.id).one()
    # Recargar no mueve la fecha: el visto es cuando se entero.
    assert recibo.opened_at == primera
    assert ShiftWeekReceipt.query.count() == 1


# ── Confirmar ─────────────────────────────────────────────────────────────────

def test_confirmar_queda_registrado(client, db, ana, m1, app):
    _asignar(db, LUNES, m1, ana)
    token = _token(app, ana.id)

    res = client.post(f'/horario/{token}/confirmar')
    assert res.status_code in (301, 302)

    recibo = ShiftWeekReceipt.query.filter_by(cleaner_id=ana.id).one()
    assert recibo.accepted_at is not None
    assert recibo.estado == 'aceptado'
    # Quien confirma sin abrir (desde el propio boton) tambien cuenta como visto.
    assert recibo.opened_at is not None


def test_confirmar_dos_veces_no_mueve_la_fecha(client, db, ana, m1, app):
    _asignar(db, LUNES, m1, ana)
    token = _token(app, ana.id)

    client.post(f'/horario/{token}/confirmar')
    primera = ShiftWeekReceipt.query.filter_by(cleaner_id=ana.id).one().accepted_at

    client.post(f'/horario/{token}/confirmar')
    db.session.expire_all()
    recibos = ShiftWeekReceipt.query.filter_by(cleaner_id=ana.id).all()
    assert len(recibos) == 1
    assert recibos[0].accepted_at == primera


def test_la_pagina_ya_confirmada_no_vuelve_a_ofrecer_el_boton(client, db, ana,
                                                              m1, app):
    _asignar(db, LUNES, m1, ana)
    token = _token(app, ana.id)
    client.post(f'/horario/{token}/confirmar')

    texto = client.get('/horario/' + token).get_data(as_text=True)
    assert 'Ya lo has confirmado' in texto
    assert 'lo acepto' not in texto


def test_confirmar_con_un_token_invalido_no_escribe_nada(client, db, ana):
    res = client.post('/horario/invento/confirmar')
    assert res.status_code == 404
    assert ShiftWeekReceipt.query.count() == 0


# ── El envio ──────────────────────────────────────────────────────────────────

def test_sin_configuracion_no_se_llama_a_meta(auth_client, db, ana, m1,
                                              monkeypatch, app):
    _asignar(db, LUNES, m1, ana)
    app.config['WHATSAPP_TOKEN'] = None

    llamadas = []
    import requests
    monkeypatch.setattr(requests, 'post',
                        lambda *a, **k: llamadas.append(a) or _Respuesta(200, {}))

    res = auth_client.post('/cuadrantes/tablero/enviar',
                           json={'week_start': LUNES.isoformat()})
    assert res.status_code == 400
    assert 'WHATSAPP_TOKEN' in res.get_json()['error']
    assert llamadas == []
    assert ShiftWeekReceipt.query.count() == 0


def test_sin_direccion_publica_no_se_gasta_el_mensaje(auth_client, db, ana, m1,
                                                      con_envio, monkeypatch):
    """El enlace saldria con el host interno: mejor no mandarlo."""
    _asignar(db, LUNES, m1, ana)
    con_envio.config['PUBLIC_BASE_URL'] = ''

    llamadas = []
    import requests
    monkeypatch.setattr(requests, 'post',
                        lambda *a, **k: llamadas.append(a) or _Respuesta(200, {}))

    res = auth_client.post('/cuadrantes/tablero/enviar',
                           json={'week_start': LUNES.isoformat()})
    assert res.status_code == 400
    assert 'PUBLIC_BASE_URL' in res.get_json()['error']
    assert llamadas == []


def test_un_envio_correcto_queda_apuntado(auth_client, db, ana, m1, con_envio,
                                          monkeypatch):
    _asignar(db, LUNES, m1, ana)

    enviados = []

    def falso_post(url, json=None, timeout=None, headers=None):
        enviados.append({'url': url, 'cuerpo': json})
        return _Respuesta(200, {'messages': [{'id': 'wamid.ABC'}]})

    import requests
    monkeypatch.setattr(requests, 'post', falso_post)

    res = auth_client.post('/cuadrantes/tablero/enviar',
                           json={'week_start': LUNES.isoformat()})
    assert res.status_code == 200
    datos = res.get_json()
    assert datos['enviados'] == 1 and datos['fallidos'] == 0

    recibo = ShiftWeekReceipt.query.filter_by(cleaner_id=ana.id).one()
    assert recibo.sent_at is not None
    assert recibo.provider_id == 'wamid.ABC'
    assert recibo.channel == 'whatsapp'
    assert recibo.error is None
    assert recibo.estado == 'enviado'

    # El mensaje lleva el enlace, no el horario: viaja por un servicio externo.
    parametros = [p['text'] for p in
                  enviados[0]['cuerpo']['template']['components'][0]['parameters']]
    assert parametros[0] == 'Ana Pons'
    assert parametros[2].startswith('https://ejemplo.test:8444/horario/')
    assert '07:30' not in ' '.join(parametros)
    assert enviados[0]['cuerpo']['to'] == '34612345678'


def test_si_meta_lo_rechaza_se_guarda_el_motivo_y_no_el_envio(
        auth_client, db, ana, m1, con_envio, monkeypatch):
    _asignar(db, LUNES, m1, ana)

    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: _Respuesta(
        400, {'error': {'message': 'Template name does not exist'}}))

    res = auth_client.post('/cuadrantes/tablero/enviar',
                           json={'week_start': LUNES.isoformat()})
    assert res.status_code == 200
    assert res.get_json()['fallidos'] == 1

    recibo = ShiftWeekReceipt.query.filter_by(cleaner_id=ana.id).one()
    assert recibo.sent_at is None
    assert 'Template name does not exist' in recibo.error
    assert recibo.estado == 'error'


def test_si_whatsapp_no_contesta_tampoco_cuenta_como_enviado(
        auth_client, db, ana, m1, con_envio, monkeypatch):
    import requests

    def se_cae(*a, **k):
        raise requests.RequestException('timeout')

    _asignar(db, LUNES, m1, ana)
    monkeypatch.setattr(requests, 'post', se_cae)

    auth_client.post('/cuadrantes/tablero/enviar',
                     json={'week_start': LUNES.isoformat()})
    recibo = ShiftWeekReceipt.query.filter_by(cleaner_id=ana.id).one()
    assert recibo.sent_at is None
    assert 'conectar' in recibo.error


def test_sin_telefono_se_dice_cual_falta(auth_client, db, m1, con_envio,
                                         monkeypatch):
    sin_tel = _trabajadora(db, 'Carmen Roig', 'carmen', telefono='')
    _asignar(db, LUNES, m1, sin_tel)

    import requests
    monkeypatch.setattr(requests, 'post',
                        lambda *a, **k: pytest.fail('no deberia llamar a Meta'))

    res = auth_client.post('/cuadrantes/tablero/enviar',
                           json={'week_start': LUNES.isoformat()})
    datos = res.get_json()
    assert datos['fallidos'] == 1
    assert datos['detalle'][0]['name'] == 'Carmen Roig'

    recibo = ShiftWeekReceipt.query.filter_by(cleaner_id=sin_tel.id).one()
    assert 'telefono' in recibo.error
    assert recibo.sent_at is None


def test_sin_lista_se_manda_solo_a_quien_falta(auth_client, db, ana, berta,
                                               m1, m2, con_envio, monkeypatch):
    """Cada mensaje se paga: lo que ya salio no se repite por su cuenta."""
    _asignar(db, LUNES, m1, ana)
    _asignar(db, LUNES, m2, berta)
    db.session.add(ShiftWeekReceipt(week_start=LUNES, cleaner_id=ana.id,
                                    sent_at=datetime.now(), channel='whatsapp'))
    db.session.commit()

    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: _Respuesta(
        200, {'messages': [{'id': 'wamid.X'}]}))

    datos = auth_client.post('/cuadrantes/tablero/enviar',
                             json={'week_start': LUNES.isoformat()}).get_json()
    assert datos['enviados'] == 1
    assert [d['name'] for d in datos['detalle']] == ['Berta Lluch']


def test_no_se_manda_a_quien_no_tiene_turno_esa_semana(auth_client, db, ana,
                                                       berta, m1, con_envio,
                                                       monkeypatch):
    _asignar(db, LUNES, m1, ana)

    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: _Respuesta(
        200, {'messages': [{'id': 'wamid.X'}]}))

    datos = auth_client.post(
        '/cuadrantes/tablero/enviar',
        json={'week_start': LUNES.isoformat(),
              'cleaner_ids': [ana.id, berta.id]}).get_json()
    assert datos['enviados'] == 1
    assert [d['name'] for d in datos['detalle']] == ['Ana Pons']


def test_una_semana_mal_escrita_se_rechaza(auth_client, db, con_envio):
    res = auth_client.post('/cuadrantes/tablero/enviar',
                           json={'week_start': 'el lunes'})
    assert res.status_code == 400
    assert 'Semana' in res.get_json()['error']


# ── Autorizacion ──────────────────────────────────────────────────────────────

def test_enviar_sin_sesion_no_se_puede(client, db):
    res = client.post('/cuadrantes/tablero/enviar',
                      json={'week_start': LUNES.isoformat()})
    assert res.status_code in (301, 302, 401, 403)


def test_enviar_como_trabajadora_no_se_puede(client, db, ana):
    client.post('/admin/login', data={'username': 'ana', 'password': 'x123456'},
                follow_redirects=True)
    res = client.post('/cuadrantes/tablero/enviar',
                      json={'week_start': LUNES.isoformat()})
    assert res.status_code in (301, 302, 401, 403)
    assert ShiftWeekReceipt.query.count() == 0


def test_la_ruta_de_enviar_lleva_tope_de_ritmo(app):
    """Cada llamada cuesta dinero y sale a un tercero: no puede ir sin tope."""
    from app import limiter
    vista = app.view_functions['shifts.shift_board_enviar']
    clave = f'{vista.__module__}.{vista.__qualname__}.{vista.__name__}'
    assert limiter.limit_manager._decorated_limits.get(clave),         'shift_board_enviar llama a WhatsApp y no tiene @limiter.limit'


# ── La pantalla de envios ─────────────────────────────────────────────────────

def test_la_pantalla_de_envios_avisa_si_falta_la_configuracion(auth_client, db,
                                                               ana, m1, app):
    _asignar(db, LUNES, m1, ana)
    app.config['WHATSAPP_TOKEN'] = None

    texto = auth_client.get(
        '/cuadrantes/tablero/envios?semana=' + LUNES.isoformat()
    ).get_data(as_text=True)
    assert 'todavia no esta en marcha' in texto
    # Y deja el respaldo de siempre: abrir WhatsApp a mano.
    assert 'wa.me/34612345678' in texto


def test_la_pantalla_ensena_el_numero_tal_y_como_se_va_a_mandar(auth_client, db,
                                                                ana, m1,
                                                                con_envio):
    """Un numero mal escrito tiene que verse antes de gastar un mensaje."""
    _asignar(db, LUNES, m1, ana)
    texto = auth_client.get(
        '/cuadrantes/tablero/envios?semana=' + LUNES.isoformat()
    ).get_data(as_text=True)
    assert '+34612345678' in texto


def test_la_pantalla_marca_a_quien_le_falta_el_telefono(auth_client, db, m1,
                                                        con_envio):
    sin_tel = _trabajadora(db, 'Carmen Roig', 'carmen', telefono='no tiene')
    _asignar(db, LUNES, m1, sin_tel)
    texto = auth_client.get(
        '/cuadrantes/tablero/envios?semana=' + LUNES.isoformat()
    ).get_data(as_text=True)
    assert 'No se entiende el numero' in texto


def test_la_pantalla_sin_nadie_asignado_lo_dice(auth_client, db, con_envio):
    texto = auth_client.get(
        '/cuadrantes/tablero/envios?semana=' + LUNES.isoformat()
    ).get_data(as_text=True)
    assert 'no tiene a nadie asignado' in texto


def test_el_estado_de_cada_una_se_ve_en_la_pantalla(auth_client, db, ana, m1,
                                                    con_envio):
    _asignar(db, LUNES, m1, ana)
    db.session.add(ShiftWeekReceipt(
        week_start=LUNES, cleaner_id=ana.id, sent_at=datetime.now(),
        channel='whatsapp', accepted_at=datetime.now(),
        opened_at=datetime.now()))
    db.session.commit()

    texto = auth_client.get(
        '/cuadrantes/tablero/envios?semana=' + LUNES.isoformat()
    ).get_data(as_text=True)
    assert 'Aceptado' in texto


def test_la_semana_ensena_cuantas_han_confirmado(auth_client, db, ana, berta,
                                                 m1, m2):
    _asignar(db, LUNES, m1, ana)
    _asignar(db, LUNES, m2, berta)
    db.session.add(ShiftWeekReceipt(week_start=LUNES, cleaner_id=ana.id,
                                    sent_at=datetime.now(),
                                    accepted_at=datetime.now()))
    db.session.commit()

    texto = auth_client.get(
        '/cuadrantes/tablero?semana=' + LUNES.isoformat()
    ).get_data(as_text=True)
    assert '1 de 2 han confirmado' in texto
