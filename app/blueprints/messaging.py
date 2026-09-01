"""Mensajeria interna entre usuarios de la aplicacion.

Es la primera funcionalidad que comparten la PWA de trabajadoras y el panel de
administracion, porque una conversacion tiene de un lado a una trabajadora con
su JWT y del otro a una gestora con su cookie de sesion. De ahi el decorador
`dual_auth` en vez de duplicar cada ruta.

Invariante del modulo: **ninguna consulta que devuelva mensajes o adjuntos se
escribe sin pasar por `_require_membership()`**. Es lo unico que sostiene el
requisito de que el administrador no lee conversaciones ajenas, asi que no hay
atajos: tambien las rutas de solo lectura pasan por ahi.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from .. import app, db, limiter
from ..models import (
    Cleaner, Conversation, ConversationMember, Message, MessageAttachment,
)
from ..utils import (
    admin_required, dual_auth, current_dual_user, _safe_commit, log_audit,
    _allowed_file, _save_image_stream, _sniff_media,
    ALLOWED_IMAGE_EXTENSIONS, AUDIO_MIME_EXTENSIONS, VIDEO_MIME_EXTENSIONS,
)

bp = Blueprint('messaging', __name__)

PREVIEW_MAX = 140
PAGE_SIZE = 50
DELETE_WINDOW_MINUTES = 60


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _require_membership(conversation_id: int):
    """Devuelve mi ConversationMember, o None si no tengo nada que hacer aqui.

    Quien llama responde **404**, nunca 403: un 403 confirmaria que esa
    conversacion existe, y eso ya es informacion sobre conversaciones ajenas.
    """
    user = current_dual_user()
    if not user:
        return None
    return ConversationMember.query.filter_by(
        conversation_id=conversation_id, cleaner_id=user.id).first()


def _visible_messages(member: ConversationMember):
    """Consulta base de los mensajes que este miembro puede ver.

    Recorta por los dos cortes propios: lo vaciado por el (`cleared_before_id`)
    y, si salio de un grupo, lo dicho despues de irse (`left_at_message_id`).
    """
    q = Message.query.filter(
        Message.conversation_id == member.conversation_id,
        Message.id > member.cleared_before_id,
    )
    if member.left_at is not None:
        q = q.filter(Message.id <= member.left_at_message_id)
    return q


def _unread_counts(cleaner_id: int) -> dict:
    """{conversation_id: no_leidos} de todas mis conversaciones, en una consulta.

    Sin esto la lista de conversaciones haria una consulta por fila.
    """
    rows = (
        db.session.query(Message.conversation_id, func.count(Message.id))
        .join(ConversationMember,
              ConversationMember.conversation_id == Message.conversation_id)
        .filter(
            ConversationMember.cleaner_id == cleaner_id,
            Message.id > ConversationMember.last_read_message_id,
            Message.id > ConversationMember.cleared_before_id,
            Message.sender_id != cleaner_id,
            Message.deleted_at.is_(None),
        )
        .group_by(Message.conversation_id)
        .all()
    )
    return {cid: n for cid, n in rows}


def _read_watermarks(conversation_id: int, me_id: int) -> tuple:
    """(visto_por_todos, visto_por_alguien) segun las marcas de los demas.

    De estos dos enteros salen los ticks de todo el hilo, sin guardar una fila
    de "leido" por mensaje y por persona.
    """
    rows = (
        db.session.query(ConversationMember.last_read_message_id)
        .filter(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.cleaner_id != me_id,
            ConversationMember.left_at.is_(None),
        ).all()
    )
    valores = [r[0] for r in rows]
    if not valores:
        return 0, 0
    return min(valores), max(valores)


def _preview_for(msg: Message) -> str:
    """Resumen de una linea para la lista de conversaciones."""
    if msg.deleted_at:
        return 'Mensaje eliminado'
    etiquetas = {'image': '📷 Foto', 'audio': '🎤 Nota de voz', 'video': '🎬 Vídeo'}
    if msg.kind in etiquetas:
        return etiquetas[msg.kind]
    return (msg.body or '')[:PREVIEW_MAX]


def _touch_conversation(conv: Conversation, msg: Message) -> None:
    conv.last_message_at = msg.created_at
    conv.last_message_id = msg.id
    conv.last_message_preview = _preview_for(msg)


def _peer_of(conv: Conversation, me_id: int):
    """La otra persona de una conversacion 1-a-1."""
    otro = ConversationMember.query.filter(
        ConversationMember.conversation_id == conv.id,
        ConversationMember.cleaner_id != me_id,
    ).first()
    return otro.cleaner if otro else None


def _conversation_title(conv: Conversation, me_id: int) -> str:
    if conv.kind == 'group':
        return conv.title or 'Grupo'
    peer = _peer_of(conv, me_id)
    return peer.name if peer else 'Conversación'


def _message_json(msg: Message) -> dict:
    if msg.deleted_at:
        return {
            'id': msg.id, 'sender_id': msg.sender_id, 'kind': msg.kind,
            'body': None, 'deleted': True,
            'created_at': msg.created_at.isoformat(), 'attachment': None,
        }
    adj = msg.attachments[0] if msg.attachments else None
    return {
        'id': msg.id,
        'sender_id': msg.sender_id,
        'sender_name': msg.sender.name if msg.sender else '',
        'kind': msg.kind,
        'body': msg.body,
        'deleted': False,
        'created_at': msg.created_at.isoformat(),
        'attachment': {
            'id': adj.id, 'media_type': adj.media_type,
            'duration_seconds': adj.duration_seconds,
            'width': adj.width, 'height': adj.height,
            'has_thumb': bool(adj.thumb_path),
        } if adj else None,
    }


def _my_conversations(cleaner_id: int, archived: bool = False):
    return (
        db.session.query(Conversation, ConversationMember)
        .join(ConversationMember,
              ConversationMember.conversation_id == Conversation.id)
        .filter(
            ConversationMember.cleaner_id == cleaner_id,
            ConversationMember.archived == archived,
            Conversation.is_active.is_(True),
        )
        .options(joinedload(Conversation.members))
        .order_by(Conversation.last_message_at.desc().nullslast(),
                  Conversation.id.desc())
    )


def _serialize_conversations(pares, unread: dict, me_id: int) -> list:
    salida = []
    for conv, member in pares:
        salida.append({
            'id': conv.id,
            'kind': conv.kind,
            'title': _conversation_title(conv, me_id),
            'last_message_preview': conv.last_message_preview,
            'last_message_at': conv.last_message_at.isoformat() if conv.last_message_at else None,
            'unread': unread.get(conv.id, 0),
            'muted': bool(member.muted_until and member.muted_until > datetime.now()),
            'left': member.left_at is not None,
            'member_count': len([m for m in conv.members if m.left_at is None]),
        })
    return salida


# ══════════════════════════════════════════════════════════════════════════════
#  CONVERSACIONES
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/api/messaging/conversations', methods=['GET'])
@dual_auth
def list_conversations():
    me = current_dual_user()
    archivadas = request.args.get('archived') == '1'
    pares = _my_conversations(me.id, archived=archivadas).all()
    unread = _unread_counts(me.id)
    return jsonify(_serialize_conversations(pares, unread, me.id)), 200


@bp.route('/api/messaging/conversations/direct', methods=['POST'])
@limiter.limit('30/minute')
@dual_auth
def open_direct():
    """Abre (o recupera) la conversacion 1-a-1 con otra persona."""
    me = current_dual_user()
    data = request.json or {}
    peer_id = data.get('peer_id')
    if not isinstance(peer_id, int) or peer_id == me.id:
        return jsonify({'error': 'Destinatario no válido'}), 400

    peer = db.session.get(Cleaner, peer_id)
    if not peer or not peer.active:
        return jsonify({'error': 'Destinatario no válido'}), 400

    clave = Conversation.direct_key(me.id, peer_id)
    conv = Conversation.query.filter_by(dm_key=clave).first()
    if conv:
        return jsonify({'conversation_id': conv.id, 'created': False}), 200

    conv = Conversation(kind='direct', dm_key=clave, created_by=me.id)
    db.session.add(conv)
    db.session.flush()
    db.session.add(ConversationMember(conversation_id=conv.id, cleaner_id=me.id))
    db.session.add(ConversationMember(conversation_id=conv.id, cleaner_id=peer_id))

    try:
        db.session.commit()
    except IntegrityError:
        # Los dos moviles han pulsado a la vez: gana el que llego primero y este
        # se queda con la conversacion que ya existe.
        db.session.rollback()
        conv = Conversation.query.filter_by(dm_key=clave).first()
        if not conv:
            return jsonify({'error': 'No se pudo abrir la conversación'}), 500
        return jsonify({'conversation_id': conv.id, 'created': False}), 200

    return jsonify({'conversation_id': conv.id, 'created': True}), 201


@bp.route('/api/messaging/contacts', methods=['GET'])
@dual_auth
def list_contacts():
    me = current_dual_user()
    q = (request.args.get('q') or '').strip()
    consulta = Cleaner.query.filter(Cleaner.active.is_(True), Cleaner.id != me.id)
    if q:
        consulta = consulta.filter(Cleaner.name.ilike(f'%{q}%'))
    personas = consulta.order_by(Cleaner.name).limit(100).all()
    return jsonify([
        {'id': c.id, 'name': c.name, 'role': c.role} for c in personas
    ]), 200


# ══════════════════════════════════════════════════════════════════════════════
#  MENSAJES
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/api/messaging/conversations/<int:cid>/messages', methods=['GET'])
@dual_auth
def list_messages(cid: int):
    member = _require_membership(cid)
    if not member:
        return jsonify({'error': 'No encontrado'}), 404

    q = _visible_messages(member)
    before_id = request.args.get('before_id', type=int)
    after_id = request.args.get('after_id', type=int)
    if before_id:
        q = q.filter(Message.id < before_id)
    if after_id:
        q = q.filter(Message.id > after_id)

    limite = min(request.args.get('limit', PAGE_SIZE, type=int) or PAGE_SIZE, PAGE_SIZE)
    filas = (q.options(joinedload(Message.sender), joinedload(Message.attachments))
              .order_by(Message.id.desc()).limit(limite + 1).all())
    hay_mas = len(filas) > limite
    filas = list(reversed(filas[:limite]))

    visto_todos, visto_alguien = _read_watermarks(cid, member.cleaner_id)
    return jsonify({
        'messages': [_message_json(m) for m in filas],
        'has_more': hay_mas,
        'read_min': visto_todos,
        'read_max': visto_alguien,
        'my_last_read': member.last_read_message_id,
    }), 200


@bp.route('/api/messaging/conversations/<int:cid>/messages', methods=['POST'])
@limiter.limit('120/minute')
@dual_auth
def send_message(cid: int):
    member = _require_membership(cid)
    if not member or member.left_at is not None:
        return jsonify({'error': 'No encontrado'}), 404

    data = request.json or {}
    cuerpo = (data.get('body') or '').strip()
    if not cuerpo:
        return jsonify({'error': 'El mensaje está vacío'}), 400
    if len(cuerpo) > 4000:
        return jsonify({'error': 'El mensaje es demasiado largo'}), 400

    client_uuid = (data.get('client_uuid') or '').strip() or None
    if client_uuid:
        # Reenvio tras un corte de cobertura: devolver el mismo mensaje en vez
        # de duplicarlo.
        previo = Message.query.filter_by(
            sender_id=member.cleaner_id, client_uuid=client_uuid).first()
        if previo:
            return jsonify(_message_json(previo)), 200

    msg = Message(conversation_id=cid, sender_id=member.cleaner_id,
                  kind='text', body=cuerpo, client_uuid=client_uuid)
    db.session.add(msg)
    db.session.flush()

    conv = db.session.get(Conversation, cid)
    _touch_conversation(conv, msg)
    # Quien escribe ha leido lo suyo.
    member.last_read_message_id = msg.id
    member.last_read_at = datetime.now()

    ok, err = _safe_commit('Error al enviar el mensaje')
    if not ok:
        return jsonify({'error': err}), 500

    _notify_new_message(conv, msg)
    return jsonify(_message_json(msg)), 201


@bp.route('/api/messaging/messages/<int:mid>/delete', methods=['POST'])
@dual_auth
def delete_message(mid: int):
    me = current_dual_user()
    msg = db.session.get(Message, mid)
    if not msg:
        return jsonify({'error': 'No encontrado'}), 404
    if not _require_membership(msg.conversation_id):
        return jsonify({'error': 'No encontrado'}), 404
    if msg.sender_id != me.id:
        return jsonify({'error': 'Solo puedes eliminar tus propios mensajes'}), 403
    if msg.deleted_at:
        return jsonify({'ok': True}), 200
    if datetime.now() - msg.created_at > timedelta(minutes=DELETE_WINDOW_MINUTES):
        return jsonify({
            'error': f'Solo se puede eliminar durante los primeros {DELETE_WINDOW_MINUTES} minutos'
        }), 400

    _delete_attachment_files(msg)
    msg.deleted_at = datetime.now()
    msg.deleted_by_id = me.id
    msg.body = None

    conv = db.session.get(Conversation, msg.conversation_id)
    if conv and conv.last_message_id == msg.id:
        conv.last_message_preview = 'Mensaje eliminado'

    ok, err = _safe_commit('Error al eliminar el mensaje')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True}), 200


def _delete_attachment_files(msg: Message) -> None:
    """Borra del disco los ficheros de un mensaje. Definido aqui desde la fase 1
    para que el borrado no deje huerfanos cuando lleguen los adjuntos."""
    import os
    for adj in list(msg.attachments):
        for rel in (adj.file_path, adj.thumb_path):
            if not rel:
                continue
            try:
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], rel))
            except OSError:
                pass
        db.session.delete(adj)


# ══════════════════════════════════════════════════════════════════════════════
#  ESTADO POR MIEMBRO
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/api/messaging/conversations/<int:cid>/read', methods=['POST'])
@dual_auth
def mark_read(cid: int):
    member = _require_membership(cid)
    if not member:
        return jsonify({'error': 'No encontrado'}), 404

    data = request.json or {}
    hasta = data.get('up_to_id')
    if not isinstance(hasta, int):
        return jsonify({'error': 'Falta el mensaje hasta el que se ha leído'}), 400

    # La marca nunca retrocede: dos pestanas abiertas o un poll que llega tarde
    # no pueden resucitar mensajes ya leidos.
    if hasta > member.last_read_message_id:
        member.last_read_message_id = hasta
        member.last_read_at = datetime.now()
        ok, err = _safe_commit('Error al marcar como leído')
        if not ok:
            return jsonify({'error': err}), 500

    return jsonify({'ok': True, 'unread': _unread_counts(member.cleaner_id).get(cid, 0)}), 200


@bp.route('/api/messaging/conversations/<int:cid>/mute', methods=['POST'])
@dual_auth
def mute_conversation(cid: int):
    member = _require_membership(cid)
    if not member:
        return jsonify({'error': 'No encontrado'}), 404
    horas = (request.json or {}).get('hours', 0)
    if not isinstance(horas, int) or horas < 0 or horas > 24 * 30:
        return jsonify({'error': 'Duración no válida'}), 400
    member.muted_until = datetime.now() + timedelta(hours=horas) if horas else None
    ok, err = _safe_commit('Error al silenciar la conversación')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True}), 200


@bp.route('/api/messaging/conversations/<int:cid>/clear', methods=['POST'])
@dual_auth
def clear_conversation(cid: int):
    """Vacia la conversacion **solo para quien lo pide**."""
    member = _require_membership(cid)
    if not member:
        return jsonify({'error': 'No encontrado'}), 404
    ultimo = db.session.query(func.max(Message.id)).filter(
        Message.conversation_id == cid).scalar() or 0
    member.cleared_before_id = ultimo
    if ultimo > member.last_read_message_id:
        member.last_read_message_id = ultimo
    ok, err = _safe_commit('Error al vaciar la conversación')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True}), 200


@bp.route('/api/messaging/conversations/<int:cid>/archive', methods=['POST'])
@dual_auth
def archive_conversation(cid: int):
    member = _require_membership(cid)
    if not member:
        return jsonify({'error': 'No encontrado'}), 404
    member.archived = bool((request.json or {}).get('archived', True))
    ok, err = _safe_commit('Error al archivar la conversación')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True}), 200


# ══════════════════════════════════════════════════════════════════════════════
#  SONDEO
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/api/messaging/poll', methods=['GET'])
@dual_auth
def poll():
    """Delta desde `cursor`. Sin limite de peticiones a proposito.

    Los contadores de Flask-Limiter viven en memoria y por worker (hay dos), asi
    que un limite aqui daria 429 aleatorios segun a que worker caiga el sondeo.
    El coste real de esta ruta son dos consultas agregadas indexadas y, cuando
    no hay novedades, una respuesta de ~40 bytes.
    """
    me = current_dual_user()
    cursor = request.args.get('cursor', 0, type=int)
    cid = request.args.get('cid', type=int)
    since = request.args.get('since')

    ultimo = db.session.query(func.max(Message.id)).join(
        ConversationMember,
        ConversationMember.conversation_id == Message.conversation_id,
    ).filter(ConversationMember.cleaner_id == me.id).scalar() or 0

    unread = _unread_counts(me.id)
    total = sum(unread.values())

    if ultimo <= cursor and not cid:
        return jsonify({'cursor': ultimo, 'changed': False, 'total_unread': total}), 200

    pares = [
        (conv, member) for conv, member in _my_conversations(me.id).all()
        if (conv.last_message_id or 0) > cursor
    ]
    salida = {
        'cursor': ultimo,
        'total_unread': total,
        'conversations': _serialize_conversations(pares, unread, me.id),
    }

    if cid:
        member = _require_membership(cid)
        if not member:
            return jsonify({'error': 'No encontrado'}), 404
        nuevos = (_visible_messages(member)
                  .filter(Message.id > cursor)
                  .options(joinedload(Message.sender), joinedload(Message.attachments))
                  .order_by(Message.id).limit(PAGE_SIZE).all())
        salida['messages'] = [_message_json(m) for m in nuevos]
        visto_todos, visto_alguien = _read_watermarks(cid, me.id)
        salida['read_min'] = visto_todos
        salida['read_max'] = visto_alguien

        # Un mensaje borrado no cambia de id, asi que no entra por `id > cursor`:
        # sin esto la lapida no llegaria nunca a quien ya tenia el hilo abierto.
        if since:
            try:
                desde = datetime.fromisoformat(since)
            except ValueError:
                desde = None
            if desde:
                borrados = _visible_messages(member).filter(
                    Message.deleted_at.isnot(None), Message.deleted_at >= desde).all()
                salida['updated'] = [{'id': m.id, 'deleted': True} for m in borrados]

    # `changed` dice si hay algo que pintar, no si venia un `cid`. Con la
    # conversacion abierta y en silencio tiene que salir False, o el cliente no
    # afloja nunca el ritmo del sondeo.
    salida['changed'] = bool(salida['conversations']
                             or salida.get('messages')
                             or salida.get('updated'))
    return jsonify(salida), 200


# ══════════════════════════════════════════════════════════════════════════════
#  AVISOS
# ══════════════════════════════════════════════════════════════════════════════

PUSH_COALESCE_MINUTES = 5


def _notify_new_message(conv: Conversation, msg: Message) -> None:
    """Aviso push a los demas miembros.

    El cuerpo **nunca lleva el texto del mensaje**: lo escribe una trabajadora y
    puede nombrar a un residente, y el push viaja por un servicio externo. Ver
    la regla de seguridad sobre datos de salud.

    `send_push_to_worker` es sincrono y hace un POST por suscripcion dentro de
    esta peticion, asi que se agrupa: como mucho un aviso cada cinco minutos por
    persona y conversacion. El resto se ve al abrir la app.
    """
    from .notifications import send_push_to_worker

    ahora = datetime.now()
    corte = ahora - timedelta(minutes=PUSH_COALESCE_MINUTES)
    titulo = conv.title or (msg.sender.name if msg.sender else 'Mensaje nuevo')
    cuerpo = ('Nuevo mensaje en el grupo' if conv.kind == 'group'
              else 'Te ha enviado un mensaje')

    destinatarios = ConversationMember.query.filter(
        ConversationMember.conversation_id == conv.id,
        ConversationMember.cleaner_id != msg.sender_id,
        ConversationMember.left_at.is_(None),
    ).all()

    avisados = False
    for m in destinatarios:
        if m.muted_until and m.muted_until > ahora:
            continue
        if m.last_push_at and m.last_push_at > corte:
            continue
        m.last_push_at = ahora
        avisados = True
        try:
            send_push_to_worker(m.cleaner_id, titulo, cuerpo,
                                url=f'/worker?chat={conv.id}', tag=f'chat-{conv.id}')
        except Exception as e:  # el aviso nunca puede tumbar el envio
            app.logger.error('Error al enviar el aviso de mensaje: %s', e)

    if avisados:
        ok, err = _safe_commit('Error al registrar el aviso')
        if not ok:
            app.logger.error('No se pudo registrar el aviso de mensaje: %s', err)


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL DE ADMINISTRACION
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/admin/mensajes')
@admin_required
def admin_messaging():
    """Pagina del panel. Los datos los pide por `/api/messaging/...`, que
    comprueba pertenencia: desde aqui no se ve ninguna conversacion ajena."""
    return render_template('admin_messaging.html')


# ══════════════════════════════════════════════════════════════════════════════
#  GRUPOS
# ══════════════════════════════════════════════════════════════════════════════

def _puede_gestionar_grupos(user) -> bool:
    """Quien crea y administra los grupos.

    Los 1-a-1 los abre cualquiera; los grupos no, para que no proliferen
    duplicados que luego nadie limpia. Un grupo es un canal medio oficial de la
    residencia ("Turno de tarde"), asi que lo monta coordinacion.
    """
    return bool(user.is_admin)


def _add_system_message(conv: Conversation, actor_id: int, texto: str) -> Message:
    """Deja constancia en el hilo de un cambio en el grupo.

    Sin esto, alguien desaparece del grupo y nadie sabe si se fue o lo sacaron.
    """
    msg = Message(conversation_id=conv.id, sender_id=actor_id,
                  kind='system', body=texto)
    db.session.add(msg)
    db.session.flush()
    _touch_conversation(conv, msg)
    return msg


def _miembros_activos(cid: int):
    return ConversationMember.query.filter(
        ConversationMember.conversation_id == cid,
        ConversationMember.left_at.is_(None),
    ).all()


def _ultimo_mensaje_id(cid: int) -> int:
    return db.session.query(func.max(Message.id)).filter(
        Message.conversation_id == cid).scalar() or 0


def _incorporar(conv: Conversation, cleaner_id: int, role: str = 'member'):
    """Alta de un miembro, reutilizando su fila si ya estuvo dentro.

    Al reincorporarse no se le devuelve lo que se hablo mientras estaba fuera:
    se entro en un grupo, no se abre un archivo.
    """
    m = ConversationMember.query.filter_by(
        conversation_id=conv.id, cleaner_id=cleaner_id).first()
    ultimo = _ultimo_mensaje_id(conv.id)
    if m:
        if m.left_at is None:
            return None                      # ya estaba dentro
        m.left_at = None
        m.left_at_message_id = 0
        m.joined_at = datetime.now()
        m.cleared_before_id = ultimo
        m.last_read_message_id = max(m.last_read_message_id, ultimo)
        m.archived = False
    else:
        m = ConversationMember(
            conversation_id=conv.id, cleaner_id=cleaner_id, role=role,
            cleared_before_id=ultimo, last_read_message_id=ultimo)
        db.session.add(m)
    return m


@bp.route('/api/messaging/conversations/group', methods=['POST'])
@limiter.limit('10/minute')
@dual_auth
def create_group():
    me = current_dual_user()
    if not _puede_gestionar_grupos(me):
        return jsonify({'error': 'Solo la administración puede crear grupos'}), 403

    data = request.json or {}
    titulo = (data.get('title') or '').strip()
    if not titulo:
        return jsonify({'error': 'Ponle un nombre al grupo'}), 400
    if len(titulo) > 100:
        return jsonify({'error': 'El nombre del grupo es demasiado largo'}), 400

    ids = {i for i in (data.get('member_ids') or []) if isinstance(i, int)}
    ids.discard(me.id)
    personas = Cleaner.query.filter(Cleaner.id.in_(ids), Cleaner.active.is_(True)).all() if ids else []
    if not personas:
        return jsonify({'error': 'Elige al menos una persona para el grupo'}), 400

    conv = Conversation(kind='group', title=titulo, created_by=me.id)
    db.session.add(conv)
    db.session.flush()
    db.session.add(ConversationMember(conversation_id=conv.id, cleaner_id=me.id, role='owner'))
    for p in personas:
        db.session.add(ConversationMember(conversation_id=conv.id, cleaner_id=p.id))

    _add_system_message(conv, me.id, f'{me.name} ha creado el grupo «{titulo}»')
    log_audit('create', 'conversation', conv.id,
              {'nombre': titulo, 'miembros': sorted(p.id for p in personas)})
    ok, err = _safe_commit('Error al crear el grupo')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'conversation_id': conv.id}), 201


@bp.route('/api/messaging/conversations/<int:cid>/info', methods=['GET'])
@dual_auth
def conversation_info(cid: int):
    member = _require_membership(cid)
    if not member:
        return jsonify({'error': 'No encontrado'}), 404
    conv = db.session.get(Conversation, cid)
    me = current_dual_user()

    miembros = (ConversationMember.query
                .filter(ConversationMember.conversation_id == cid,
                        ConversationMember.left_at.is_(None))
                .options(joinedload(ConversationMember.cleaner)).all())
    return jsonify({
        'id': conv.id,
        'kind': conv.kind,
        'title': _conversation_title(conv, me.id),
        'muted': bool(member.muted_until and member.muted_until > datetime.now()),
        'archived': member.archived,
        'left': member.left_at is not None,
        'can_manage': conv.kind == 'group' and (
            member.role == 'owner' or _puede_gestionar_grupos(me)),
        'members': [
            {'id': m.cleaner_id,
             'name': m.cleaner.name if m.cleaner else '—',
             'role': m.role,
             'is_me': m.cleaner_id == me.id}
            for m in sorted(miembros, key=lambda m: (m.role != 'owner',
                                                     m.cleaner.name if m.cleaner else ''))
        ],
    }), 200


@bp.route('/api/messaging/conversations/<int:cid>/members', methods=['POST'])
@dual_auth
def add_members(cid: int):
    me = current_dual_user()
    member = _require_membership(cid)
    conv = db.session.get(Conversation, cid)
    if not member or not conv or conv.kind != 'group':
        return jsonify({'error': 'No encontrado'}), 404
    if member.role != 'owner' and not _puede_gestionar_grupos(me):
        return jsonify({'error': 'No puedes cambiar los miembros de este grupo'}), 403

    ids = {i for i in ((request.json or {}).get('member_ids') or []) if isinstance(i, int)}
    personas = Cleaner.query.filter(Cleaner.id.in_(ids), Cleaner.active.is_(True)).all() if ids else []
    if not personas:
        return jsonify({'error': 'Elige a quién quieres añadir'}), 400

    anadidas = [p for p in personas if _incorporar(conv, p.id) is not None]
    if anadidas:
        nombres = ', '.join(p.name for p in anadidas)
        _add_system_message(conv, me.id, f'{me.name} ha añadido a {nombres}')
        log_audit('update', 'conversation', conv.id,
                  {'accion': 'anadir_miembros', 'ids': sorted(p.id for p in anadidas)})

    ok, err = _safe_commit('Error al añadir al grupo')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True, 'added': len(anadidas)}), 200


@bp.route('/api/messaging/conversations/<int:cid>/members/<int:uid>/remove', methods=['POST'])
@dual_auth
def remove_member(cid: int, uid: int):
    me = current_dual_user()
    member = _require_membership(cid)
    conv = db.session.get(Conversation, cid)
    if not member or not conv or conv.kind != 'group':
        return jsonify({'error': 'No encontrado'}), 404
    if member.role != 'owner' and not _puede_gestionar_grupos(me):
        return jsonify({'error': 'No puedes cambiar los miembros de este grupo'}), 403

    objetivo = ConversationMember.query.filter_by(
        conversation_id=cid, cleaner_id=uid).first()
    if not objetivo or objetivo.left_at is not None:
        return jsonify({'error': 'Esa persona no está en el grupo'}), 404
    if objetivo.role == 'owner':
        return jsonify({'error': 'No se puede sacar a quien creó el grupo'}), 400

    objetivo.left_at = datetime.now()
    objetivo.left_at_message_id = _ultimo_mensaje_id(cid)
    persona = db.session.get(Cleaner, uid)
    _add_system_message(conv, me.id,
                        f'{me.name} ha sacado del grupo a {persona.name if persona else "alguien"}')
    log_audit('update', 'conversation', conv.id, {'accion': 'quitar_miembro', 'id': uid})

    ok, err = _safe_commit('Error al sacar del grupo')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True}), 200


@bp.route('/api/messaging/conversations/<int:cid>/leave', methods=['POST'])
@dual_auth
def leave_group(cid: int):
    me = current_dual_user()
    member = _require_membership(cid)
    conv = db.session.get(Conversation, cid)
    if not member or not conv:
        return jsonify({'error': 'No encontrado'}), 404
    if conv.kind != 'group':
        # De un 1-a-1 no se sale: se archiva, que es lo que espera la gente.
        return jsonify({'error': 'De una conversación individual no se puede salir'}), 400
    if member.left_at is not None:
        return jsonify({'ok': True}), 200

    member.left_at = datetime.now()
    member.left_at_message_id = _ultimo_mensaje_id(cid)
    _add_system_message(conv, me.id, f'{me.name} ha salido del grupo')

    ok, err = _safe_commit('Error al salir del grupo')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True}), 200


# ── Gestion de grupos desde el panel ─────────────────────────────────────────

@bp.route('/admin/mensajes/grupos')
@admin_required
def admin_messaging_groups():
    """Lista de grupos. Muestra nombre y miembros, nunca el contenido del hilo."""
    grupos = (Conversation.query
              .filter(Conversation.kind == 'group')
              .options(joinedload(Conversation.members)
                       .joinedload(ConversationMember.cleaner))
              .order_by(Conversation.is_active.desc(),
                        Conversation.last_message_at.desc().nullslast(),
                        Conversation.id.desc())
              .all())
    return render_template(
        'admin_messaging_groups.html',
        grupos=grupos,
        personas=Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all(),
    )


@bp.route('/admin/mensajes/grupos/<int:cid>/renombrar', methods=['POST'])
@admin_required
def admin_rename_group(cid: int):
    conv = db.session.get(Conversation, cid)
    if not conv or conv.kind != 'group':
        flash('Grupo no encontrado.', 'danger')
        return redirect(request.referrer or url_for('messaging.admin_messaging_groups'))

    titulo = (request.form.get('title') or '').strip()
    if not titulo:
        flash('Ponle un nombre al grupo.', 'warning')
        return redirect(request.referrer or url_for('messaging.admin_messaging_groups'))

    anterior = conv.title
    conv.title = titulo[:100]
    if anterior != conv.title:
        _add_system_message(conv, current_user.id,
                            f'{current_user.name} ha renombrado el grupo a «{conv.title}»')
    log_audit('update', 'conversation', conv.id,
              {'accion': 'renombrar', 'antes': anterior, 'despues': conv.title})
    ok, error = _safe_commit('Error al renombrar el grupo')
    flash(error if not ok else 'Grupo actualizado.', 'danger' if not ok else 'success')
    return redirect(request.referrer or url_for('messaging.admin_messaging_groups'))


@bp.route('/admin/mensajes/grupos/<int:cid>/eliminar', methods=['POST'])
@admin_required
def admin_delete_group(cid: int):
    """Borra el grupo entero: mensajes, adjuntos y ficheros.

    Archivar es lo que se quiere para un grupo que se ha usado, porque a los
    miembros les puede hacer falta consultarlo. Esto es para el otro caso: el
    grupo creado por error o de prueba, que archivado se queda ahi para
    siempre ensuciando la lista. No se puede deshacer, asi que la pantalla lo
    advierte y queda registrado en la auditoria.
    """
    import os

    conv = db.session.get(Conversation, cid)
    if not conv or conv.kind != 'group':
        flash('Grupo no encontrado.', 'danger')
        return redirect(request.referrer or url_for('messaging.admin_messaging_groups'))

    titulo = conv.title
    mensajes = Message.query.filter_by(conversation_id=cid).all()
    ficheros = 0
    for msg in mensajes:
        for adj in list(msg.attachments):
            for rel in (adj.file_path, adj.thumb_path):
                if not rel:
                    continue
                try:
                    os.remove(os.path.join(app.config['UPLOAD_FOLDER'], rel))
                    ficheros += 1
                except OSError:
                    pass
            db.session.delete(adj)
        db.session.delete(msg)
    ConversationMember.query.filter_by(conversation_id=cid).delete(synchronize_session=False)
    db.session.delete(conv)

    log_audit('delete', 'conversation', cid,
              {'nombre': titulo, 'mensajes': len(mensajes), 'ficheros': ficheros})
    ok, error = _safe_commit('Error al eliminar el grupo')
    if not ok:
        flash(error, 'danger')
    else:
        flash(f'Grupo «{titulo}» eliminado.', 'success')
    return redirect(request.referrer or url_for('messaging.admin_messaging_groups'))


@bp.route('/admin/mensajes/grupos/<int:cid>/archivar', methods=['POST'])
@admin_required
def admin_archive_group(cid: int):
    """Retira el grupo de circulacion sin borrar lo que se dijo.

    Borrarlo dejaria a los miembros sin el historial de un canal que quiza haga
    falta consultar; archivarlo solo lo saca de la lista.
    """
    conv = db.session.get(Conversation, cid)
    if not conv or conv.kind != 'group':
        flash('Grupo no encontrado.', 'danger')
        return redirect(request.referrer or url_for('messaging.admin_messaging_groups'))

    conv.is_active = not conv.is_active
    log_audit('update', 'conversation', conv.id,
              {'accion': 'archivar' if not conv.is_active else 'reactivar'})
    ok, error = _safe_commit('Error al archivar el grupo')
    if not ok:
        flash(error, 'danger')
    else:
        flash('Grupo archivado.' if not conv.is_active else 'Grupo reactivado.', 'success')
    return redirect(request.referrer or url_for('messaging.admin_messaging_groups'))


# ══════════════════════════════════════════════════════════════════════════════
#  ADJUNTOS
# ══════════════════════════════════════════════════════════════════════════════
#
# Las fotos se reprocesan con Pillow (abrir, corregir EXIF, convertir y volver a
# escribir): eso descarta cualquier carga util escondida en el fichero original y
# es la defensa de la que habla la regla de seguridad. El audio y el video no se
# pueden reprocesar sin ffmpeg, que no esta en la imagen y no compensa meterlo
# (250 MB mas y transcodificar dentro de un worker de gunicorn), asi que se
# guardan tal cual y la defensa son tres capas: extension derivada de un mapa
# cerrado de MIME, firma binaria comprobada, y al servirlos, Content-Type de
# lista blanca con `X-Content-Type-Options: nosniff`, que es lo que impide de
# verdad que el navegador interprete el fichero como otra cosa.

MEDIA_LIMITES = {
    'image': 8 * 1024 * 1024,
    'audio': 5 * 1024 * 1024,
    'video': 15 * 1024 * 1024,
}
AUDIO_MAX_SEGUNDOS = 120
VIDEO_MAX_SEGUNDOS = 20


def _carpeta_adjuntos(cid: int) -> str:
    """uploads/messaging/<AAAA>/<MM>/<conversacion>.

    Partido por mes para que purgar y excluir del backup sea un `rm -rf` de una
    carpeta, en vez de recorrer la tabla fichero a fichero.
    """
    import os
    ahora = datetime.now()
    return os.path.join(app.config['UPLOAD_FOLDER'], 'messaging',
                        f'{ahora.year:04d}', f'{ahora.month:02d}', str(cid))


def _rel(ruta_abs: str) -> str:
    import os
    return os.path.relpath(ruta_abs, app.config['UPLOAD_FOLDER']).replace(os.sep, '/')


@bp.route('/api/messaging/conversations/<int:cid>/attachments', methods=['POST'])
@limiter.limit('20/minute')
@dual_auth
def send_attachment(cid: int):
    import os
    from io import BytesIO
    from PIL import UnidentifiedImageError

    member = _require_membership(cid)
    if not member or member.left_at is not None:
        return jsonify({'error': 'No encontrado'}), 404

    fichero = request.files.get('file')
    if not fichero or not fichero.filename:
        return jsonify({'error': 'No se ha recibido ningún archivo'}), 400

    tipo = (request.form.get('media_type') or '').strip()
    if tipo not in MEDIA_LIMITES:
        return jsonify({'error': 'Tipo de archivo no admitido'}), 400

    datos = fichero.read()
    if not datos:
        return jsonify({'error': 'El archivo está vacío'}), 400
    if len(datos) > MEDIA_LIMITES[tipo]:
        topes = {'image': '8 MB', 'audio': '5 MB', 'video': '15 MB'}
        return jsonify({'error': f'El archivo supera el máximo de {topes[tipo]}'}), 400

    carpeta = _carpeta_adjuntos(cid)
    marca = datetime.now().strftime('%Y%m%d%H%M%S%f')
    base = f'{member.cleaner_id}_{marca}'

    ancho = alto = None
    thumb_rel = None
    try:
        if tipo == 'image':
            if not _allowed_file(fichero.filename, ALLOWED_IMAGE_EXTENSIONS):
                return jsonify({'error': 'Formato de imagen no admitido'}), 400
            nombre, ancho, alto = _save_image_stream(
                BytesIO(datos), carpeta, f'img_{base}', max_side=1600, quality=82)
            mime = 'image/jpeg'
            ruta_rel = _rel(os.path.join(carpeta, nombre))
            # Miniatura para la burbuja: abrir la grande en cada mensaje del hilo
            # es lo que hace que un chat con fotos se coma los datos del movil.
            mini, _w, _h = _save_image_stream(
                BytesIO(datos), carpeta, f'img_{base}_t', max_side=320, quality=75)
            thumb_rel = _rel(os.path.join(carpeta, mini))
        else:
            mapa = AUDIO_MIME_EXTENSIONS if tipo == 'audio' else VIDEO_MIME_EXTENSIONS
            declarado = (fichero.mimetype or '').split(';')[0].strip().lower()
            ext = mapa.get(declarado)
            if not ext:
                return jsonify({'error': 'Formato no admitido'}), 400
            if not _sniff_media(datos[:16], tipo):
                # El fichero no es lo que dice ser.
                return jsonify({'error': 'El archivo no es válido'}), 400
            os.makedirs(carpeta, exist_ok=True)
            prefijo = 'aud' if tipo == 'audio' else 'vid'
            nombre = f'{prefijo}_{base}.{ext}'
            with open(os.path.join(carpeta, nombre), 'wb') as fh:
                fh.write(datos)
            mime = declarado
            ruta_rel = _rel(os.path.join(carpeta, nombre))

            # El poster del video lo saca el movil del primer fotograma: sin
            # ffmpeg el servidor no puede, y una burbuja negra no dice nada.
            poster = request.form.get('poster')
            if tipo == 'video' and poster:
                try:
                    import base64 as _b64
                    crudo = poster.split(',', 1)[1] if ',' in poster else poster
                    mini, _w, _h = _save_image_stream(
                        BytesIO(_b64.b64decode(crudo)), carpeta, f'vid_{base}_t',
                        max_side=320, quality=75)
                    thumb_rel = _rel(os.path.join(carpeta, mini))
                except Exception:
                    thumb_rel = None      # se pinta un icono generico
    except UnidentifiedImageError:
        # Hereda de OSError, asi que sin esta rama un fichero que no es una
        # imagen se contaba como averia del servidor (500) en vez de decirle a
        # la trabajadora lo unico util: que ese archivo no vale.
        return jsonify({'error': 'El archivo no es una imagen válida'}), 400
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except (OSError, IOError) as e:
        app.logger.error('Error al guardar el adjunto: %s', e)
        return jsonify({'error': 'No se pudo guardar el archivo'}), 500
    except Exception as e:
        app.logger.error('Adjunto no valido: %s', e)
        return jsonify({'error': 'El archivo no es válido'}), 400

    duracion = request.form.get('duration', type=int)
    tope = AUDIO_MAX_SEGUNDOS if tipo == 'audio' else VIDEO_MAX_SEGUNDOS
    if tipo != 'image' and duracion and duracion > tope:
        return jsonify({'error': f'La grabación supera los {tope} segundos'}), 400

    client_uuid = (request.form.get('client_uuid') or '').strip() or None
    if client_uuid:
        previo = Message.query.filter_by(
            sender_id=member.cleaner_id, client_uuid=client_uuid).first()
        if previo:
            return jsonify(_message_json(previo)), 200

    msg = Message(conversation_id=cid, sender_id=member.cleaner_id, kind=tipo,
                  body=(request.form.get('body') or '').strip() or None,
                  client_uuid=client_uuid)
    db.session.add(msg)
    db.session.flush()
    db.session.add(MessageAttachment(
        message_id=msg.id, media_type=tipo, file_path=ruta_rel, thumb_path=thumb_rel,
        mime_type=mime, size_bytes=len(datos), duration_seconds=duracion,
        width=ancho, height=alto, original_filename=fichero.filename[:255]))

    conv = db.session.get(Conversation, cid)
    _touch_conversation(conv, msg)
    member.last_read_message_id = msg.id
    member.last_read_at = datetime.now()

    ok, err = _safe_commit('Error al enviar el archivo')
    if not ok:
        return jsonify({'error': err}), 500

    db.session.refresh(msg)
    _notify_new_message(conv, msg)
    return jsonify(_message_json(msg)), 201


@bp.route('/api/messaging/attachments/<int:aid>', methods=['GET'])
@dual_auth
def serve_attachment(aid: int):
    """Sirve un adjunto solo a quien esta en esa conversacion.

    Acepta un id y nunca una ruta, asi que el path traversal no es posible por
    construccion. Cualquier motivo para no servirlo responde 404 y no 403: un
    403 confirmaria que ese adjunto existe, que ya es informacion sobre una
    conversacion ajena.
    """
    import os
    from flask import send_file

    adj = db.session.get(MessageAttachment, aid)
    if not adj:
        return jsonify({'error': 'No encontrado'}), 404

    msg = db.session.get(Message, adj.message_id)
    if not msg or msg.deleted_at:
        return jsonify({'error': 'No encontrado'}), 404

    member = _require_membership(msg.conversation_id)
    if not member:
        return jsonify({'error': 'No encontrado'}), 404
    if msg.id <= member.cleared_before_id:
        return jsonify({'error': 'No encontrado'}), 404
    if member.left_at is not None and msg.id > member.left_at_message_id:
        return jsonify({'error': 'No encontrado'}), 404

    rel = adj.thumb_path if (request.args.get('thumb') == '1' and adj.thumb_path) else adj.file_path
    ruta = os.path.normpath(os.path.join(app.config['UPLOAD_FOLDER'], rel))
    if not ruta.startswith(os.path.normpath(app.config['UPLOAD_FOLDER'])) or not os.path.exists(ruta):
        return jsonify({'error': 'No encontrado'}), 404

    # El tipo sale de la lista blanca del servidor, nunca de lo que declaro el
    # cliente, y con `nosniff` el navegador no puede reinterpretarlo.
    mime = 'image/jpeg' if rel == adj.thumb_path else adj.mime_type
    resp = send_file(ruta, mimetype=mime, conditional=True)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Content-Disposition'] = 'inline'
    resp.headers['Cache-Control'] = 'private, max-age=86400'
    return resp
