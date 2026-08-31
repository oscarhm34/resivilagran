"""
test_messaging.py — Mensajeria interna entre usuarios.

El requisito que sostiene todo lo demas: **nadie lee una conversacion de la que
no es miembro**, y eso incluye al administrador. En una residencia las
conversaciones hablan de residentes, asi que son datos de salud; que el panel de
administracion no sea una puerta trasera no es una preferencia, es el motivo por
el que la mensajeria puede existir dentro de esta aplicacion.

El resto de los tests fijan el mecanismo que hace barato el chat: las marcas de
agua enteras de `ConversationMember`. Si alguien las sustituye por filas de
"leido" por mensaje, estos tests siguen pasando; si las rompe, no.
"""

from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import Cleaner, Conversation, ConversationMember, Message


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def second_cleaner(db):
    user = Cleaner(username='limpiadora2', name='Ana Torres', is_admin=False)
    user.set_password('limpia123')
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def second_headers(db, second_cleaner, app):
    with app.app_context():
        token = create_access_token(identity=second_cleaner.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def direct_conversation(db, cleaner_user, second_cleaner):
    """Conversacion entre las dos trabajadoras. El admin queda fuera."""
    conv = Conversation(kind='direct',
                        dm_key=Conversation.direct_key(cleaner_user.id, second_cleaner.id),
                        created_by=cleaner_user.id)
    db.session.add(conv)
    db.session.flush()
    db.session.add(ConversationMember(conversation_id=conv.id, cleaner_id=cleaner_user.id))
    db.session.add(ConversationMember(conversation_id=conv.id, cleaner_id=second_cleaner.id))
    db.session.commit()
    return conv


def _enviar(client, headers, cid, texto, **extra):
    cuerpo = {'body': texto}
    cuerpo.update(extra)
    return client.post(f'/api/messaging/conversations/{cid}/messages',
                       headers=headers, json=cuerpo)


def _miembro(db, cid, cleaner_id):
    return ConversationMember.query.filter_by(
        conversation_id=cid, cleaner_id=cleaner_id).first()


# ── Autorizacion ─────────────────────────────────────────────────────────────

def test_sin_autenticar_no_hay_conversaciones(client, db):
    res = client.get('/api/messaging/conversations')

    assert res.status_code == 401


def test_la_pwa_entra_con_jwt(client, db, worker_headers):
    res = client.get('/api/messaging/conversations', headers=worker_headers)

    assert res.status_code == 200
    assert res.get_json() == []


def test_el_panel_entra_con_cookie(auth_client, db):
    """El mismo endpoint sirve a los dos mundos: es la razon de `dual_auth`."""
    res = auth_client.get('/api/messaging/conversations')

    assert res.status_code == 200


def test_usuario_desactivado_no_puede_usar_la_mensajeria(
        client, db, cleaner_user, worker_headers):
    cleaner_user.active = False
    db.session.commit()

    res = client.get('/api/messaging/conversations', headers=worker_headers)

    assert res.status_code == 403


def test_escritura_por_cookie_sin_cabecera_anticsrf_se_rechaza(
        auth_client, db, second_cleaner):
    """El blueprint esta exento de CSRF y la cookie viaja sola."""
    res = auth_client.post('/api/messaging/conversations/direct',
                           json={'peer_id': second_cleaner.id})

    assert res.status_code == 403


def test_escritura_por_cookie_con_cabecera_funciona(auth_client, db, second_cleaner):
    res = auth_client.post('/api/messaging/conversations/direct',
                           json={'peer_id': second_cleaner.id},
                           headers={'X-CSRFToken': 'x'})

    assert res.status_code == 201


# ── Cruce de identidad: lo que blinda la privacidad ──────────────────────────

def test_admin_no_puede_leer_conversacion_ajena(auth_client, db, direct_conversation):
    """Un administrador con sesion abierta sobre el hilo de dos trabajadoras."""
    res = auth_client.get(f'/api/messaging/conversations/{direct_conversation.id}/messages')

    assert res.status_code == 404


def test_admin_no_puede_escribir_en_conversacion_ajena(auth_client, db, direct_conversation):
    res = auth_client.post(
        f'/api/messaging/conversations/{direct_conversation.id}/messages',
        json={'body': 'Hola'}, headers={'X-CSRFToken': 'x'})

    assert res.status_code == 404
    assert Message.query.count() == 0


def test_no_miembro_no_ve_los_mensajes(client, db, direct_conversation,
                                       cleaner_user, worker_headers, admin_user):
    """404 y no 403: un 403 confirmaria que la conversacion existe."""
    ajena = Conversation(kind='direct', dm_key='999-1000', created_by=admin_user.id)
    db.session.add(ajena)
    db.session.commit()

    res = client.get(f'/api/messaging/conversations/{ajena.id}/messages',
                     headers=worker_headers)

    assert res.status_code == 404


def test_el_sondeo_no_filtra_conversaciones_ajenas(
        client, db, direct_conversation, cleaner_user, second_cleaner,
        worker_headers, admin_user, auth_client):
    _enviar(client, worker_headers, direct_conversation.id, 'Algo privado')

    res = auth_client.get('/api/messaging/poll?cursor=0')

    datos = res.get_json()
    assert res.status_code == 200
    assert datos['total_unread'] == 0
    assert datos.get('conversations', []) == []


# ── Puerta de atras de /uploads ──────────────────────────────────────────────

def test_uploads_generico_no_sirve_adjuntos_de_mensajeria(
        client, db, worker_headers, auth_client):
    """Esas rutas sirven cualquier fichero a cualquier autenticado."""
    por_jwt = client.get('/api/uploads/messaging/2026/08/1/img_1.jpg',
                         headers=worker_headers)
    por_cookie = auth_client.get('/uploads/messaging/2026/08/1/img_1.jpg')

    assert por_jwt.status_code == 404
    assert por_cookie.status_code == 404


# ── Conversaciones 1-a-1 ─────────────────────────────────────────────────────

def test_abrir_conversacion_directa_la_crea(client, db, second_cleaner, worker_headers):
    res = client.post('/api/messaging/conversations/direct',
                      headers=worker_headers, json={'peer_id': second_cleaner.id})

    assert res.status_code == 201
    assert res.get_json()['created'] is True
    assert Conversation.query.count() == 1


def test_conversacion_directa_no_se_duplica(client, db, second_cleaner, worker_headers):
    primera = client.post('/api/messaging/conversations/direct',
                          headers=worker_headers, json={'peer_id': second_cleaner.id})

    segunda = client.post('/api/messaging/conversations/direct',
                          headers=worker_headers, json={'peer_id': second_cleaner.id})

    assert segunda.get_json()['created'] is False
    assert segunda.get_json()['conversation_id'] == primera.get_json()['conversation_id']
    assert Conversation.query.count() == 1


def test_la_pareja_invertida_da_la_misma_conversacion(
        client, db, cleaner_user, second_cleaner, worker_headers, second_headers):
    ida = client.post('/api/messaging/conversations/direct',
                      headers=worker_headers, json={'peer_id': second_cleaner.id})

    vuelta = client.post('/api/messaging/conversations/direct',
                         headers=second_headers, json={'peer_id': cleaner_user.id})

    assert vuelta.get_json()['conversation_id'] == ida.get_json()['conversation_id']
    assert Conversation.query.count() == 1


def test_no_se_puede_abrir_conversacion_con_uno_mismo(
        client, db, cleaner_user, worker_headers):
    res = client.post('/api/messaging/conversations/direct',
                      headers=worker_headers, json={'peer_id': cleaner_user.id})

    assert res.status_code == 400


def test_no_se_puede_abrir_conversacion_con_alguien_de_baja(
        client, db, second_cleaner, worker_headers):
    second_cleaner.active = False
    db.session.commit()

    res = client.post('/api/messaging/conversations/direct',
                      headers=worker_headers, json={'peer_id': second_cleaner.id})

    assert res.status_code == 400


# ── Envio de mensajes ────────────────────────────────────────────────────────

def test_enviar_mensaje_lo_guarda_y_actualiza_la_lista(
        client, db, direct_conversation, worker_headers):
    res = _enviar(client, worker_headers, direct_conversation.id, 'Buenos días')

    assert res.status_code == 201
    assert res.get_json()['body'] == 'Buenos días'
    conv = db.session.get(Conversation, direct_conversation.id)
    assert conv.last_message_preview == 'Buenos días'
    assert conv.last_message_id == res.get_json()['id']


def test_mensaje_vacio_se_rechaza(client, db, direct_conversation, worker_headers):
    res = _enviar(client, worker_headers, direct_conversation.id, '   ')

    assert res.status_code == 400
    assert Message.query.count() == 0


def test_client_uuid_evita_duplicados(client, db, direct_conversation, worker_headers):
    """La wifi de una residencia se cae y la trabajadora vuelve a darle a enviar."""
    primero = _enviar(client, worker_headers, direct_conversation.id, 'Hola',
                      client_uuid='abc-123')

    reintento = _enviar(client, worker_headers, direct_conversation.id, 'Hola',
                        client_uuid='abc-123')

    assert reintento.get_json()['id'] == primero.get_json()['id']
    assert Message.query.count() == 1


# ── Marcas de lectura ────────────────────────────────────────────────────────

def test_no_leidos_se_calculan_por_marca_de_agua(
        client, db, direct_conversation, worker_headers, second_headers):
    for texto in ('uno', 'dos', 'tres'):
        _enviar(client, worker_headers, direct_conversation.id, texto)

    res = client.get('/api/messaging/conversations', headers=second_headers)

    assert res.get_json()[0]['unread'] == 3


def test_los_mensajes_propios_no_cuentan_como_no_leidos(
        client, db, direct_conversation, worker_headers):
    _enviar(client, worker_headers, direct_conversation.id, 'mío')

    res = client.get('/api/messaging/conversations', headers=worker_headers)

    assert res.get_json()[0]['unread'] == 0


def test_marcar_leido_baja_el_contador(
        client, db, direct_conversation, worker_headers, second_headers):
    ids = [_enviar(client, worker_headers, direct_conversation.id, t).get_json()['id']
           for t in ('uno', 'dos', 'tres')]

    res = client.post(f'/api/messaging/conversations/{direct_conversation.id}/read',
                      headers=second_headers, json={'up_to_id': ids[1]})

    assert res.get_json()['unread'] == 1


def test_la_marca_de_lectura_no_retrocede(
        client, db, direct_conversation, cleaner_user, second_cleaner,
        worker_headers, second_headers):
    """Dos pestañas abiertas o un sondeo que llega tarde no resucitan mensajes."""
    ids = [_enviar(client, worker_headers, direct_conversation.id, t).get_json()['id']
           for t in ('uno', 'dos', 'tres')]
    url = f'/api/messaging/conversations/{direct_conversation.id}/read'
    client.post(url, headers=second_headers, json={'up_to_id': ids[2]})

    client.post(url, headers=second_headers, json={'up_to_id': ids[0]})

    assert _miembro(db, direct_conversation.id, second_cleaner.id).last_read_message_id == ids[2]


def test_visto_se_deriva_de_las_marcas_de_los_demas(
        client, db, direct_conversation, worker_headers, second_headers):
    ids = [_enviar(client, worker_headers, direct_conversation.id, t).get_json()['id']
           for t in ('uno', 'dos')]
    client.post(f'/api/messaging/conversations/{direct_conversation.id}/read',
                headers=second_headers, json={'up_to_id': ids[0]})

    res = client.get(f'/api/messaging/conversations/{direct_conversation.id}/messages',
                     headers=worker_headers)

    assert res.get_json()['read_min'] == ids[0]


# ── Borrado ──────────────────────────────────────────────────────────────────

def test_solo_el_emisor_borra_su_mensaje(
        client, db, direct_conversation, worker_headers, second_headers):
    mid = _enviar(client, worker_headers, direct_conversation.id, 'mío').get_json()['id']

    res = client.post(f'/api/messaging/messages/{mid}/delete', headers=second_headers)

    assert res.status_code == 403
    assert db.session.get(Message, mid).deleted_at is None


def test_borrar_deja_lapida_sin_cuerpo(client, db, direct_conversation, worker_headers):
    mid = _enviar(client, worker_headers, direct_conversation.id, 'secreto').get_json()['id']

    client.post(f'/api/messaging/messages/{mid}/delete', headers=worker_headers)

    res = client.get(f'/api/messaging/conversations/{direct_conversation.id}/messages',
                     headers=worker_headers)
    mensajes = res.get_json()['messages']
    assert len(mensajes) == 1, 'la fila se conserva para no abrir huecos en la paginacion'
    assert mensajes[0]['deleted'] is True
    assert mensajes[0]['body'] is None


def test_no_se_puede_borrar_un_mensaje_antiguo(
        client, db, direct_conversation, worker_headers):
    mid = _enviar(client, worker_headers, direct_conversation.id, 'viejo').get_json()['id']
    db.session.get(Message, mid).created_at = datetime.now() - timedelta(hours=3)
    db.session.commit()

    res = client.post(f'/api/messaging/messages/{mid}/delete', headers=worker_headers)

    assert res.status_code == 400


# ── Vaciar para mí ───────────────────────────────────────────────────────────

def test_vaciar_solo_afecta_a_quien_lo_pide(
        client, db, direct_conversation, worker_headers, second_headers):
    _enviar(client, worker_headers, direct_conversation.id, 'hola')

    client.post(f'/api/messaging/conversations/{direct_conversation.id}/clear',
                headers=second_headers)

    mios = client.get(f'/api/messaging/conversations/{direct_conversation.id}/messages',
                      headers=worker_headers).get_json()['messages']
    suyos = client.get(f'/api/messaging/conversations/{direct_conversation.id}/messages',
                       headers=second_headers).get_json()['messages']
    assert len(mios) == 1
    assert suyos == []


# ── Paginacion y sondeo ──────────────────────────────────────────────────────

def test_el_limite_de_pagina_se_recorta(client, db, direct_conversation, worker_headers):
    for i in range(5):
        _enviar(client, worker_headers, direct_conversation.id, f'm{i}')

    res = client.get(
        f'/api/messaging/conversations/{direct_conversation.id}/messages?limit=999',
        headers=worker_headers)

    assert len(res.get_json()['messages']) == 5  # no revienta ni devuelve de mas


def test_paginar_hacia_atras_con_before_id(client, db, direct_conversation, worker_headers):
    ids = [_enviar(client, worker_headers, direct_conversation.id, f'm{i}').get_json()['id']
           for i in range(4)]

    res = client.get(
        f'/api/messaging/conversations/{direct_conversation.id}/messages?before_id={ids[2]}',
        headers=worker_headers)

    assert [m['id'] for m in res.get_json()['messages']] == ids[:2]


def test_sondeo_sin_novedades_no_devuelve_mensajes(
        client, db, direct_conversation, worker_headers):
    mid = _enviar(client, worker_headers, direct_conversation.id, 'hola').get_json()['id']

    res = client.get(f'/api/messaging/poll?cursor={mid}', headers=worker_headers)

    datos = res.get_json()
    assert datos['changed'] is False
    assert 'messages' not in datos


def test_sondeo_con_la_conversacion_abierta_y_sin_novedades_dice_que_no_cambia(
        client, db, direct_conversation, worker_headers):
    """Si dijera que si, el movil nunca aflojaria el ritmo del sondeo."""
    mid = _enviar(client, worker_headers, direct_conversation.id, 'hola').get_json()['id']

    res = client.get(
        f'/api/messaging/poll?cursor={mid}&cid={direct_conversation.id}',
        headers=worker_headers)

    assert res.get_json()['changed'] is False


def test_sondeo_devuelve_solo_lo_nuevo(
        client, db, direct_conversation, worker_headers, second_headers):
    viejo = _enviar(client, worker_headers, direct_conversation.id, 'viejo').get_json()['id']
    nuevo = _enviar(client, worker_headers, direct_conversation.id, 'nuevo').get_json()['id']

    res = client.get(
        f'/api/messaging/poll?cursor={viejo}&cid={direct_conversation.id}',
        headers=second_headers)

    assert [m['id'] for m in res.get_json()['messages']] == [nuevo]


def test_sondeo_avisa_de_los_borrados_por_since(
        client, db, direct_conversation, worker_headers, second_headers):
    """Un mensaje borrado no cambia de id, asi que no entra por `id > cursor`."""
    mid = _enviar(client, worker_headers, direct_conversation.id, 'hola').get_json()['id']
    desde = (datetime.now() - timedelta(seconds=5)).isoformat()
    client.post(f'/api/messaging/messages/{mid}/delete', headers=worker_headers)

    res = client.get(
        f'/api/messaging/poll?cursor={mid}&cid={direct_conversation.id}&since={desde}',
        headers=second_headers)

    assert res.get_json()['updated'] == [{'id': mid, 'deleted': True}]


# ── Contactos y pagina del panel ─────────────────────────────────────────────

def test_los_contactos_excluyen_a_uno_mismo_y_a_las_bajas(
        client, db, cleaner_user, second_cleaner, admin_user, worker_headers):
    admin_user.active = False
    db.session.commit()

    res = client.get('/api/messaging/contacts', headers=worker_headers)

    nombres = [c['name'] for c in res.get_json()]
    assert nombres == ['Ana Torres']


def test_la_pagina_de_mensajes_del_panel_exige_admin(client, db):
    res = client.get('/admin/mensajes')

    assert res.status_code in (302, 401, 403)


def test_la_pagina_de_mensajes_se_pinta_para_el_admin(auth_client, db):
    res = auth_client.get('/admin/mensajes')

    assert res.status_code == 200
    assert 'Solo ves las conversaciones de las que formas parte' in res.get_data(as_text=True)
