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


# ══════════════════════════════════════════════════════════════════════════
#  GRUPOS
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def third_cleaner(db):
    user = Cleaner(username='limpiadora3', name='Rosa Prieto', is_admin=False)
    user.set_password('limpia123')
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def third_headers(db, third_cleaner, app):
    with app.app_context():
        token = create_access_token(identity=third_cleaner.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def grupo(auth_client, db, admin_user, cleaner_user, second_cleaner):
    """Grupo creado por el admin con las dos trabajadoras dentro."""
    res = auth_client.post('/api/messaging/conversations/group',
                           headers={'X-CSRFToken': 'x'},
                           json={'title': 'Turno de tarde',
                                 'member_ids': [cleaner_user.id, second_cleaner.id]})
    assert res.status_code == 201, res.get_data(as_text=True)
    return db.session.get(Conversation, res.get_json()['conversation_id'])


def _sistema(db, cid):
    return [m.body for m in Message.query.filter_by(
        conversation_id=cid, kind='system').order_by(Message.id).all()]


# ── Quién puede crear grupos ────────────────────────────────────────────────

def test_una_trabajadora_no_puede_crear_grupos(
        client, db, second_cleaner, worker_headers):
    """Los 1-a-1 los abre cualquiera; los canales del centro los monta coordinación."""
    res = client.post('/api/messaging/conversations/group', headers=worker_headers,
                      json={'title': 'Mi grupo', 'member_ids': [second_cleaner.id]})

    assert res.status_code == 403
    assert Conversation.query.filter_by(kind='group').count() == 0


def test_el_admin_crea_el_grupo_con_sus_miembros(grupo, db, admin_user,
                                                 cleaner_user, second_cleaner):
    dentro = {m.cleaner_id for m in grupo.members if m.left_at is None}

    assert grupo.title == 'Turno de tarde'
    assert dentro == {admin_user.id, cleaner_user.id, second_cleaner.id}
    # Quien lo crea manda: puede sacar gente aunque deje de ser administrador.
    creador = [m for m in grupo.members if m.cleaner_id == admin_user.id][0]
    assert creador.role == 'owner'


def test_crear_un_grupo_sin_nombre_se_rechaza(auth_client, db, cleaner_user):
    res = auth_client.post('/api/messaging/conversations/group',
                           headers={'X-CSRFToken': 'x'},
                           json={'title': '  ', 'member_ids': [cleaner_user.id]})

    assert res.status_code == 400


def test_crear_un_grupo_sin_nadie_se_rechaza(auth_client, db):
    res = auth_client.post('/api/messaging/conversations/group',
                           headers={'X-CSRFToken': 'x'},
                           json={'title': 'Yo solo', 'member_ids': []})

    assert res.status_code == 400


def test_crear_un_grupo_deja_constancia_en_la_auditoria(grupo, db):
    from app.models import AuditLog
    registros = AuditLog.query.filter_by(table_name='conversation').all()

    assert len(registros) == 1
    assert registros[0].action == 'create'
    # Nunca el contenido de los mensajes, solo quién está dentro.
    assert 'Turno de tarde' in (registros[0].details or '')


# ── Conversar en grupo ──────────────────────────────────────────────────────

def test_los_miembros_ven_el_grupo_en_su_lista(client, db, grupo, worker_headers):
    res = client.get('/api/messaging/conversations', headers=worker_headers)

    fila = res.get_json()[0]
    assert fila['kind'] == 'group'
    assert fila['title'] == 'Turno de tarde'
    assert fila['member_count'] == 3


def test_quien_no_esta_en_el_grupo_no_lo_ve(client, db, grupo, third_headers):
    res = client.get(f'/api/messaging/conversations/{grupo.id}/messages',
                     headers=third_headers)

    assert res.status_code == 404


def test_un_mensaje_al_grupo_lo_reciben_los_demas(
        client, db, grupo, worker_headers, second_headers):
    _enviar(client, worker_headers, grupo.id, 'Llego diez minutos tarde')

    res = client.get('/api/messaging/conversations', headers=second_headers)

    # El aviso de creación del grupo también cuenta como novedad.
    assert res.get_json()[0]['unread'] == 2
    assert res.get_json()[0]['last_message_preview'] == 'Llego diez minutos tarde'


# ── Altas y bajas ───────────────────────────────────────────────────────────

def test_el_creador_anade_a_alguien_y_queda_anotado(
        auth_client, db, grupo, third_cleaner):
    res = auth_client.post(f'/api/messaging/conversations/{grupo.id}/members',
                           headers={'X-CSRFToken': 'x'},
                           json={'member_ids': [third_cleaner.id]})

    assert res.status_code == 200
    assert any('Rosa Prieto' in t for t in _sistema(db, grupo.id))


def test_un_miembro_normal_no_puede_anadir_gente(
        client, db, grupo, third_cleaner, worker_headers):
    res = client.post(f'/api/messaging/conversations/{grupo.id}/members',
                      headers=worker_headers, json={'member_ids': [third_cleaner.id]})

    assert res.status_code == 403


def test_quien_entra_despues_no_ve_lo_hablado_antes(
        auth_client, client, db, grupo, third_cleaner, third_headers, worker_headers):
    """Se entra en un grupo, no se abre un archivo."""
    _enviar(client, worker_headers, grupo.id, 'Esto es de antes')
    auth_client.post(f'/api/messaging/conversations/{grupo.id}/members',
                     headers={'X-CSRFToken': 'x'}, json={'member_ids': [third_cleaner.id]})

    res = client.get(f'/api/messaging/conversations/{grupo.id}/messages',
                     headers=third_headers)

    cuerpos = [m['body'] for m in res.get_json()['messages']]
    assert 'Esto es de antes' not in cuerpos


def test_sacar_del_grupo_congela_lo_que_ve_esa_persona(
        auth_client, client, db, grupo, cleaner_user, worker_headers, second_headers):
    auth_client.post(f'/api/messaging/conversations/{grupo.id}/members/{cleaner_user.id}/remove',
                     headers={'X-CSRFToken': 'x'})

    _enviar(client, second_headers, grupo.id, 'Esto ya no lo ve')
    res = client.get(f'/api/messaging/conversations/{grupo.id}/messages',
                     headers=worker_headers)

    cuerpos = [m['body'] for m in res.get_json()['messages']]
    assert 'Esto ya no lo ve' not in cuerpos


def test_quien_sale_del_grupo_no_puede_seguir_escribiendo(
        client, db, grupo, worker_headers):
    client.post(f'/api/messaging/conversations/{grupo.id}/leave', headers=worker_headers)

    res = _enviar(client, worker_headers, grupo.id, 'Una más')

    assert res.status_code == 404


def test_salir_del_grupo_deja_un_aviso_en_el_hilo(client, db, grupo, worker_headers):
    client.post(f'/api/messaging/conversations/{grupo.id}/leave', headers=worker_headers)

    assert any('ha salido del grupo' in t for t in _sistema(db, grupo.id))


def test_no_se_puede_sacar_a_quien_creo_el_grupo(auth_client, db, grupo, admin_user):
    res = auth_client.post(
        f'/api/messaging/conversations/{grupo.id}/members/{admin_user.id}/remove',
        headers={'X-CSRFToken': 'x'})

    assert res.status_code == 400


def test_de_una_conversacion_individual_no_se_sale(
        client, db, direct_conversation, worker_headers):
    res = client.post(f'/api/messaging/conversations/{direct_conversation.id}/leave',
                      headers=worker_headers)

    assert res.status_code == 400


# ── Ficha de la conversación ────────────────────────────────────────────────

def test_la_ficha_lista_a_los_participantes(client, db, grupo, worker_headers):
    res = client.get(f'/api/messaging/conversations/{grupo.id}/info',
                     headers=worker_headers)

    datos = res.get_json()
    assert datos['kind'] == 'group'
    assert len(datos['members']) == 3
    assert datos['can_manage'] is False        # miembro normal
    assert any(m['is_me'] for m in datos['members'])


def test_la_ficha_de_un_grupo_ajeno_no_se_puede_pedir(
        client, db, grupo, third_headers):
    res = client.get(f'/api/messaging/conversations/{grupo.id}/info',
                     headers=third_headers)

    assert res.status_code == 404


# ── Panel de administración ─────────────────────────────────────────────────

def test_la_pagina_de_grupos_exige_admin(client, db):
    res = client.get('/admin/mensajes/grupos')

    assert res.status_code in (302, 401, 403)


def test_la_pagina_de_grupos_lista_los_grupos_sin_su_contenido(
        auth_client, client, db, grupo, worker_headers):
    _enviar(client, worker_headers, grupo.id, 'Un secreto del grupo')

    res = auth_client.get('/admin/mensajes/grupos')

    texto = res.get_data(as_text=True)
    assert res.status_code == 200
    assert 'Turno de tarde' in texto
    assert 'Un secreto del grupo' not in texto


def test_renombrar_el_grupo_lo_anota_en_el_hilo(auth_client, db, grupo):
    auth_client.post(f'/admin/mensajes/grupos/{grupo.id}/renombrar',
                     data={'title': 'Turno de noche'}, follow_redirects=True)

    assert db.session.get(Conversation, grupo.id).title == 'Turno de noche'
    assert any('Turno de noche' in t for t in _sistema(db, grupo.id))


def test_archivar_el_grupo_lo_saca_de_la_lista_sin_borrar_nada(
        auth_client, client, db, grupo, worker_headers):
    _enviar(client, worker_headers, grupo.id, 'Queda guardado')

    auth_client.post(f'/admin/mensajes/grupos/{grupo.id}/archivar', follow_redirects=True)

    lista = client.get('/api/messaging/conversations', headers=worker_headers).get_json()
    assert lista == []
    assert Message.query.filter_by(conversation_id=grupo.id).count() > 0


# ══════════════════════════════════════════════════════════════════════════
#  AVISOS AL MOVIL
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def avisos_enviados(monkeypatch):
    """Recoge a quién se le habría mandado un aviso, sin salir a internet."""
    enviados = []

    def falso_push(worker_id, title, body, url=None, tag=None):
        enviados.append({'worker_id': worker_id, 'title': title, 'body': body,
                         'url': url, 'tag': tag})

    import app.blueprints.notifications as notif
    monkeypatch.setattr(notif, 'send_push_to_worker', falso_push)
    return enviados


def test_el_aviso_va_al_destinatario_y_no_al_que_escribe(
        client, db, direct_conversation, cleaner_user, second_cleaner,
        worker_headers, avisos_enviados):
    _enviar(client, worker_headers, direct_conversation.id, 'Hola')

    assert [a['worker_id'] for a in avisos_enviados] == [second_cleaner.id]


def test_el_aviso_no_lleva_el_texto_del_mensaje(
        client, db, direct_conversation, worker_headers, avisos_enviados):
    """El push pasa por un servicio externo y el texto puede nombrar a un residente."""
    _enviar(client, worker_headers, direct_conversation.id,
            'La 204 ha vomitado esta mañana')

    aviso = avisos_enviados[0]
    assert 'vomitado' not in aviso['body']
    assert '204' not in aviso['body']
    assert aviso['body'] == 'Te ha enviado un mensaje'


def test_el_aviso_abre_la_conversacion_correcta(
        client, db, direct_conversation, worker_headers, avisos_enviados):
    _enviar(client, worker_headers, direct_conversation.id, 'Hola')

    assert avisos_enviados[0]['url'] == f'/worker?chat={direct_conversation.id}'
    assert avisos_enviados[0]['tag'] == f'chat-{direct_conversation.id}'


def test_no_se_avisa_dos_veces_seguidas_de_la_misma_conversacion(
        client, db, direct_conversation, worker_headers, avisos_enviados):
    """Cada envio es una peticion HTTPS dentro del request: sin agrupar, un
    grupo de diez personas bloquearia medio servidor por cada mensaje."""
    _enviar(client, worker_headers, direct_conversation.id, 'uno')
    _enviar(client, worker_headers, direct_conversation.id, 'dos')
    _enviar(client, worker_headers, direct_conversation.id, 'tres')

    assert len(avisos_enviados) == 1


def test_una_conversacion_silenciada_no_avisa(
        client, db, direct_conversation, second_cleaner, second_headers,
        worker_headers, avisos_enviados):
    client.post(f'/api/messaging/conversations/{direct_conversation.id}/mute',
                headers=second_headers, json={'hours': 8})

    _enviar(client, worker_headers, direct_conversation.id, 'Hola')

    assert avisos_enviados == []


def test_quien_salio_del_grupo_deja_de_recibir_avisos(
        client, db, grupo, cleaner_user, second_cleaner, worker_headers,
        second_headers, avisos_enviados):
    client.post(f'/api/messaging/conversations/{grupo.id}/leave', headers=worker_headers)
    avisos_enviados.clear()

    _enviar(client, second_headers, grupo.id, 'Seguimos')

    assert cleaner_user.id not in [a['worker_id'] for a in avisos_enviados]


def test_dar_de_alta_el_movil_guarda_la_suscripcion(
        client, db, cleaner_user, worker_headers):
    """Sin esta fila no hay a donde mandar el aviso: es el primer sitio donde mirar."""
    from app.models import PushSubscription

    res = client.post('/api/push/subscribe', headers=worker_headers, json={
        'endpoint': 'https://fcm.googleapis.com/fcm/send/abc123',
        'keys': {'p256dh': 'clave-publica', 'auth': 'secreto'},
    })

    assert res.status_code in (200, 201)
    sub = PushSubscription.query.filter_by(worker_id=cleaner_user.id).first()
    assert sub is not None
    assert sub.endpoint == 'https://fcm.googleapis.com/fcm/send/abc123'


def test_dar_de_alta_el_mismo_movil_dos_veces_no_duplica(
        client, db, cleaner_user, worker_headers):
    from app.models import PushSubscription
    cuerpo = {'endpoint': 'https://fcm.googleapis.com/fcm/send/abc123',
              'keys': {'p256dh': 'clave-publica', 'auth': 'secreto'}}
    client.post('/api/push/subscribe', headers=worker_headers, json=cuerpo)

    client.post('/api/push/subscribe', headers=worker_headers, json=cuerpo)

    assert PushSubscription.query.count() == 1


# ══════════════════════════════════════════════════════════════════════════
#  CLAVES VAPID
# ══════════════════════════════════════════════════════════════════════════

def test_las_claves_vapid_tienen_el_formato_que_espera_el_navegador():
    """Sin este contrato no hay avisos, y el fallo es invisible.

    La generacion se rompio al actualizar `cryptography` (py_vapid pasaba la
    clase de la curva en vez de una instancia). El `except` era mudo, las claves
    no se guardaban y ningun aviso salio nunca, sin una sola linea en el log.
    """
    import base64
    from app.config import Config

    publica = Config.VAPID_PUBLIC_KEY
    privada = Config.VAPID_PRIVATE_KEY
    assert publica and privada, 'sin claves no se puede avisar a ningun movil'

    crudo = base64.urlsafe_b64decode(publica + '=' * ((4 - len(publica) % 4) % 4))
    # `applicationServerKey` exige el punto sin comprimir: 65 bytes tras el 0x04.
    assert len(crudo) == 65
    assert crudo[0] == 4
    # pywebpush firma con la privada en PEM.
    assert 'BEGIN PRIVATE KEY' in privada


# ══════════════════════════════════════════════════════════════════════════
#  ADJUNTOS
# ══════════════════════════════════════════════════════════════════════════

import io as _io
import os as _os


def _png(color=(200, 30, 30), tam=(600, 400)):
    from PIL import Image
    buf = _io.BytesIO()
    Image.new('RGB', tam, color).save(buf, 'PNG')
    buf.seek(0)
    return buf


def _webm(relleno=2048):
    """Cabecera EBML valida seguida de relleno: lo justo para pasar la firma."""
    return bytes.fromhex('1a45dfa3') + b'\x00' * relleno


def _mp4(relleno=2048):
    return b'\x00\x00\x00\x20ftypisom' + b'\x00' * relleno


def _subir(client, headers, cid, campo, nombre, datos, tipo, mime, **extra):
    data = {'media_type': tipo, 'file': (_io.BytesIO(datos) if isinstance(datos, bytes) else datos, nombre, mime)}
    data.update(extra)
    return client.post(f'/api/messaging/conversations/{cid}/attachments',
                       headers=headers, data=data, content_type='multipart/form-data')


def _ruta(rel):
    from app import app as flask_app
    return _os.path.join(flask_app.config['UPLOAD_FOLDER'], rel)


@pytest.fixture(autouse=True)
def _limpiar_adjuntos():
    """Borra del disco lo que dejen los tests de adjuntos."""
    from app import app as flask_app
    yield
    base = _os.path.join(flask_app.config['UPLOAD_FOLDER'], 'messaging')
    if _os.path.isdir(base):
        import shutil
        shutil.rmtree(base, ignore_errors=True)


# ── Imagen ──────────────────────────────────────────────────────────────────

def test_una_foto_se_reprocesa_a_jpeg_y_genera_miniatura(
        client, db, direct_conversation, worker_headers):
    """El reprocesado es la defensa: el fichero original nunca se guarda."""
    from app.models import MessageAttachment

    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'herida.png', _png().read(), 'image', 'image/png')

    assert res.status_code == 201
    adj = MessageAttachment.query.one()
    assert adj.media_type == 'image'
    assert adj.mime_type == 'image/jpeg'
    assert adj.file_path.endswith('.jpg')
    assert adj.thumb_path and adj.thumb_path.endswith('.jpg')
    assert _os.path.exists(_ruta(adj.file_path))
    assert _os.path.exists(_ruta(adj.thumb_path))
    assert adj.width and adj.width <= 1600


def test_un_ejecutable_con_nombre_de_foto_se_rechaza(
        client, db, direct_conversation, worker_headers):
    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'foto.jpg', b'MZ\x90\x00esto no es una imagen', 'image', 'image/jpeg')

    assert res.status_code == 400
    from app.models import MessageAttachment
    assert MessageAttachment.query.count() == 0


def test_una_extension_de_imagen_no_admitida_se_rechaza(
        client, db, direct_conversation, worker_headers):
    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'documento.svg', _png().read(), 'image', 'image/svg+xml')

    assert res.status_code == 400


# ── Audio y video ───────────────────────────────────────────────────────────

def test_una_nota_de_voz_se_guarda_tal_cual(
        client, db, direct_conversation, worker_headers):
    from app.models import MessageAttachment

    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'nota.webm', _webm(), 'audio', 'audio/webm', duration='12')

    assert res.status_code == 201
    adj = MessageAttachment.query.one()
    assert adj.media_type == 'audio'
    assert adj.file_path.endswith('.webm')
    assert adj.duration_seconds == 12
    assert res.get_json()['kind'] == 'audio'


def test_un_audio_con_cabecera_falsa_se_rechaza(
        client, db, direct_conversation, worker_headers):
    """Sin ffmpeg no se puede reprocesar, asi que la firma es la primera defensa."""
    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'nota.webm', b'<html><script>alert(1)</script></html>' + b'\x00' * 100,
                 'audio', 'audio/webm')

    assert res.status_code == 400
    from app.models import MessageAttachment
    assert MessageAttachment.query.count() == 0


def test_un_mime_de_audio_desconocido_se_rechaza(
        client, db, direct_conversation, worker_headers):
    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'nota.xyz', _webm(), 'audio', 'audio/vnd.inventado')

    assert res.status_code == 400


def test_un_video_se_guarda_con_su_duracion(
        client, db, direct_conversation, worker_headers):
    from app.models import MessageAttachment

    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'clip.mp4', _mp4(), 'video', 'video/mp4', duration='15')

    assert res.status_code == 201
    assert MessageAttachment.query.one().duration_seconds == 15


def test_un_video_mas_largo_del_limite_se_rechaza(
        client, db, direct_conversation, worker_headers):
    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'clip.mp4', _mp4(), 'video', 'video/mp4', duration='45')

    assert res.status_code == 400
    assert 'segundos' in res.get_json()['error']


def test_un_archivo_demasiado_grande_se_rechaza(
        client, db, direct_conversation, worker_headers):
    res = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'nota.webm', _webm(relleno=6 * 1024 * 1024), 'audio', 'audio/webm')

    assert res.status_code == 400
    assert 'MB' in res.get_json()['error']


# ── Servir el adjunto: es donde vive la privacidad ──────────────────────────

def test_el_adjunto_llega_a_quien_esta_en_la_conversacion(
        client, db, direct_conversation, worker_headers, second_headers):
    aid = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'foto.png', _png().read(), 'image', 'image/png').get_json()['attachment']['id']

    res = client.get(f'/api/messaging/attachments/{aid}', headers=second_headers)

    assert res.status_code == 200
    assert res.headers['X-Content-Type-Options'] == 'nosniff'
    assert res.headers['Content-Type'].startswith('image/jpeg')


def test_el_admin_no_puede_descargar_un_adjunto_ajeno(
        auth_client, client, db, direct_conversation, worker_headers):
    """El caso que sostiene todo: el panel no es una puerta de atras."""
    aid = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'foto.png', _png().read(), 'image', 'image/png').get_json()['attachment']['id']

    res = auth_client.get(f'/api/messaging/attachments/{aid}')

    assert res.status_code == 404


def test_un_adjunto_borrado_deja_de_servirse(
        client, db, direct_conversation, worker_headers):
    envio = _subir(client, worker_headers, direct_conversation.id, 'file',
                   'foto.png', _png().read(), 'image', 'image/png').get_json()
    client.post(f"/api/messaging/messages/{envio['id']}/delete", headers=worker_headers)

    res = client.get(f"/api/messaging/attachments/{envio['attachment']['id']}",
                     headers=worker_headers)

    assert res.status_code == 404


def test_borrar_el_mensaje_elimina_los_ficheros_del_disco(
        client, db, direct_conversation, worker_headers):
    from app.models import MessageAttachment
    envio = _subir(client, worker_headers, direct_conversation.id, 'file',
                   'foto.png', _png().read(), 'image', 'image/png').get_json()
    adj = MessageAttachment.query.one()
    grande, mini = _ruta(adj.file_path), _ruta(adj.thumb_path)
    assert _os.path.exists(grande)

    client.post(f"/api/messaging/messages/{envio['id']}/delete", headers=worker_headers)

    assert not _os.path.exists(grande)
    assert not _os.path.exists(mini)
    assert MessageAttachment.query.count() == 0


def test_la_miniatura_se_sirve_aparte(
        client, db, direct_conversation, worker_headers):
    aid = _subir(client, worker_headers, direct_conversation.id, 'file',
                 'foto.png', _png().read(), 'image', 'image/png').get_json()['attachment']['id']

    grande = client.get(f'/api/messaging/attachments/{aid}', headers=worker_headers)
    mini = client.get(f'/api/messaging/attachments/{aid}?thumb=1', headers=worker_headers)

    assert grande.status_code == mini.status_code == 200
    assert len(mini.data) < len(grande.data)


def test_no_se_puede_adjuntar_en_una_conversacion_ajena(
        client, db, direct_conversation, third_headers):
    res = _subir(client, third_headers, direct_conversation.id, 'file',
                 'foto.png', _png().read(), 'image', 'image/png')

    assert res.status_code == 404


def test_el_resumen_de_la_lista_dice_que_es_una_foto(
        client, db, direct_conversation, worker_headers, second_headers):
    _subir(client, worker_headers, direct_conversation.id, 'file',
           'foto.png', _png().read(), 'image', 'image/png')

    fila = client.get('/api/messaging/conversations', headers=second_headers).get_json()[0]

    assert 'Foto' in fila['last_message_preview']


# ══════════════════════════════════════════════════════════════════════════
#  PURGA
# ══════════════════════════════════════════════════════════════════════════

def _correr_purga(app, *args):
    runner = app.test_cli_runner()
    return runner.invoke(args=['purge-messages'] + list(args))


def test_la_purga_en_simulacion_no_borra_nada(
        app, client, db, direct_conversation, worker_headers):
    from datetime import timedelta
    from app.models import MessageAttachment
    envio = _subir(client, worker_headers, direct_conversation.id, 'file',
                   'foto.png', _png().read(), 'image', 'image/png').get_json()
    msg = db.session.get(Message, envio['id'])
    msg.created_at = datetime.now() - timedelta(days=400)
    db.session.commit()
    ruta = _ruta(MessageAttachment.query.one().file_path)

    _correr_purga(app, '--dry-run')

    assert _os.path.exists(ruta)
    assert Message.query.count() == 1


def test_la_purga_borra_el_adjunto_caducado_y_deja_lapida(
        app, client, db, direct_conversation, worker_headers):
    """El fichero es lo que ocupa; la fila se queda para no abrir huecos."""
    from datetime import timedelta
    from app.models import MessageAttachment
    envio = _subir(client, worker_headers, direct_conversation.id, 'file',
                   'foto.png', _png().read(), 'image', 'image/png').get_json()
    msg = db.session.get(Message, envio['id'])
    msg.created_at = datetime.now() - timedelta(days=200)   # adjunto caducado
    db.session.commit()
    ruta = _ruta(MessageAttachment.query.one().file_path)

    _correr_purga(app)

    assert not _os.path.exists(ruta)
    assert MessageAttachment.query.count() == 0
    assert db.session.get(Message, envio['id']).deleted_at is not None


def test_la_purga_no_toca_lo_reciente(
        app, client, db, direct_conversation, worker_headers):
    from app.models import MessageAttachment
    _subir(client, worker_headers, direct_conversation.id, 'file',
           'foto.png', _png().read(), 'image', 'image/png')

    _correr_purga(app)

    assert MessageAttachment.query.count() == 1
    assert Message.query.count() == 1


def test_la_purga_elimina_los_mensajes_mas_viejos_que_la_retencion(
        app, client, db, direct_conversation, worker_headers):
    from datetime import timedelta
    mid = _enviar(client, worker_headers, direct_conversation.id, 'De hace mucho').get_json()['id']
    db.session.get(Message, mid).created_at = datetime.now() - timedelta(days=400)
    db.session.commit()

    _correr_purga(app)

    assert db.session.get(Message, mid) is None
