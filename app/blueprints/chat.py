"""Chat and audio transcription blueprint."""
from __future__ import annotations

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required

from .. import app, limiter
from ..utils import admin_required

bp = Blueprint('chat', __name__)


@bp.route('/api/chat', methods=['POST'])
@limiter.limit("10/minute")
@jwt_required()
def api_chat():
    from ..chatbot import chat
    api_key = app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'Chatbot no configurado (falta ANTHROPIC_API_KEY)'}), 503
    data = request.json or {}
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Mensaje vacío'}), 400
    try:
        response = chat(message, api_key)
        return jsonify({'response': response}), 200
    except Exception as e:
        app.logger.error('Error del chatbot: %s', e)
        return jsonify({'error': 'Error del chatbot: no se pudo procesar la consulta'}), 500


@bp.route('/api/transcribe', methods=['POST'])
@limiter.limit("20/minute")
@jwt_required()
def api_transcribe():
    return _transcribe_audio()


@bp.route('/admin/transcribe', methods=['POST'])
@limiter.limit("20/minute")
@admin_required
def admin_transcribe():
    return _transcribe_audio()


@bp.route('/admin/chat', methods=['POST'])
@limiter.limit("10/minute")
@admin_required
def admin_chat():
    from ..chatbot import chat
    api_key = app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'Chatbot no configurado (falta ANTHROPIC_API_KEY)'}), 503
    data = request.json or {}
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Mensaje vacío'}), 400
    try:
        response = chat(message, api_key, is_admin=True)
        return jsonify({'response': response}), 200
    except Exception as e:
        app.logger.error('Error del chatbot: %s', e)
        return jsonify({'error': 'Error del chatbot: no se pudo procesar la consulta'}), 500


def _transcribe_audio():
    api_key = app.config.get('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'error': 'Transcripción no configurada (falta OPENAI_API_KEY)'}), 503
    audio_file = request.files.get('audio')
    if not audio_file:
        return jsonify({'error': 'No se recibió audio'}), 400
    try:
        import requests as req
        resp = req.post(
            'https://api.openai.com/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {api_key}'},
            files={'file': (audio_file.filename or 'audio.webm', audio_file.stream, audio_file.content_type or 'audio/webm')},
            data={'model': 'whisper-1', 'language': 'es'},
            timeout=30,
        )
        if resp.status_code != 200:
            app.logger.error('La API de transcripcion devolvio %s', resp.status_code)
            return jsonify({'error': 'Error en la transcripción'}), 500
        text = resp.json().get('text', '')
        return jsonify({'text': text}), 200
    except Exception as e:
        app.logger.error('Error en la transcripcion: %s', e)
        return jsonify({'error': 'Error en la transcripción'}), 500
