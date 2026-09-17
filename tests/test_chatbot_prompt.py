"""
test_chatbot_prompt.py — El prompt del chatbot se ve y se amplía desde Configuración.

El prompt base es código y no se toca desde el panel; lo que la dirección puede
añadir son instrucciones propias, que se guardan en `AppSetting` y se pegan al
final. Aquí se comprueba esa composición y la pantalla que la edita.
"""

import pytest

from app.chatbot import (SYSTEM_PROMPT, CHATBOT_EXTRA_KEY, MAX_EXTRA_PROMPT,
                         system_prompt)
from app.models import AppSetting


# ── Composición del prompt ────────────────────────────────────────────────────

def test_sin_instrucciones_adicionales_devuelve_el_prompt_base(db):
    assert system_prompt() == SYSTEM_PROMPT


def test_las_instrucciones_adicionales_se_anaden_al_final(db):
    AppSetting.set(CHATBOT_EXTRA_KEY, 'La sala grande es el comedor.')

    resultado = system_prompt()

    assert resultado.startswith(SYSTEM_PROMPT)
    assert resultado.endswith('La sala grande es el comedor.')
    assert 'Instrucciones adicionales de la residencia:' in resultado


def test_unas_instrucciones_en_blanco_no_ensucian_el_prompt(db):
    AppSetting.set(CHATBOT_EXTRA_KEY, '   \n  ')

    assert system_prompt() == SYSTEM_PROMPT


def test_las_instrucciones_muy_largas_se_recortan(db):
    AppSetting.set(CHATBOT_EXTRA_KEY, 'x' * (MAX_EXTRA_PROMPT + 500))

    anadido = system_prompt()[len(SYSTEM_PROMPT):]

    assert anadido.count('x') == MAX_EXTRA_PROMPT


def test_un_texto_largo_cabe_en_app_setting(db):
    """La columna era VARCHAR(500) y estas instrucciones no cabian."""
    largo = 'a' * MAX_EXTRA_PROMPT
    AppSetting.set(CHATBOT_EXTRA_KEY, largo)

    assert AppSetting.get(CHATBOT_EXTRA_KEY) == largo


# ── La pantalla de Configuración ──────────────────────────────────────────────

def test_configuracion_exige_administrador(client):
    respuesta = client.get('/admin/settings')

    assert respuesta.status_code in (302, 401, 403)


def test_configuracion_muestra_el_prompt_y_las_herramientas(auth_client):
    respuesta = auth_client.get('/admin/settings')
    html = respuesta.get_data(as_text=True)

    assert respuesta.status_code == 200
    assert 'Eres un asistente de la residencia' in html
    assert 'buscar_residente' in html          # una de las herramientas
    assert 'chatbot_extra_prompt' in html      # el campo editable


def test_guardar_instrucciones_adicionales(auth_client, db):
    respuesta = auth_client.post('/admin/settings', data={
        'chatbot_extra_prompt': '  Responde siempre en frases cortas.  ',
        'session_max_minutes': '120',
        'hidden_logout_minutes': '30',
        'min_session_seconds': '60',
    }, follow_redirects=True)

    assert respuesta.status_code == 200
    assert AppSetting.get(CHATBOT_EXTRA_KEY) == 'Responde siempre en frases cortas.'
    assert system_prompt().endswith('Responde siempre en frases cortas.')


def test_vaciar_el_campo_devuelve_el_prompt_de_fabrica(auth_client, db):
    AppSetting.set(CHATBOT_EXTRA_KEY, 'Algo que luego se quita.')

    auth_client.post('/admin/settings', data={
        'chatbot_extra_prompt': '',
        'session_max_minutes': '120',
        'hidden_logout_minutes': '30',
        'min_session_seconds': '60',
    }, follow_redirects=True)

    assert AppSetting.get(CHATBOT_EXTRA_KEY) == ''
    assert system_prompt() == SYSTEM_PROMPT


def test_guardar_el_prompt_no_pisa_los_demas_ajustes(auth_client, db):
    auth_client.post('/admin/settings', data={
        'chatbot_extra_prompt': 'Una linea.',
        'nfc_only': '1',
        'session_max_minutes': '90',
        'hidden_logout_minutes': '15',
        'min_session_seconds': '30',
    }, follow_redirects=True)

    assert AppSetting.get('nfc_only') == 'true'
    assert AppSetting.get('session_max_minutes') == '90'
    assert AppSetting.get('hidden_logout_minutes') == '15'
    assert AppSetting.get('min_session_seconds') == '30'


# ── Lo que llega de verdad a la llamada al modelo ─────────────────────────────

class _RespuestaFalsa:
    stop_reason = 'end_turn'

    class _Bloque:
        text = 'Listo.'

    content = [_Bloque()]


def test_las_instrucciones_llegan_a_la_llamada_al_modelo(db, monkeypatch):
    """El unico sitio donde esto importa: el `system` que sale hacia la API."""
    import app.chatbot as chatbot
    AppSetting.set(CHATBOT_EXTRA_KEY, 'Responde siempre con un refran.')
    enviados = {}

    class _ClienteFalso:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kwargs):
            enviados.update(kwargs)
            return _RespuestaFalsa()

    monkeypatch.setattr(chatbot, 'Anthropic', _ClienteFalso)

    respuesta = chatbot.chat('Hola', api_key='sk-test')

    assert respuesta == 'Listo.'
    assert enviados['system'].startswith(SYSTEM_PROMPT)
    assert enviados['system'].endswith('Responde siempre con un refran.')
