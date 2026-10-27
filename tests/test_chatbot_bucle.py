"""
test_chatbot_bucle.py — El bucle de herramientas del asistente.

Nada de esto llama a la API: se sustituye el cliente por uno falso que devuelve
respuestas preparadas y guarda lo que se le pidio. Asi los tests son gratis y
deterministas, que es la unica forma de poder probar el bucle.

Lo que se fija aqui son los fallos que tenia el bucle antiguo:
  - no habia historial, asi que "y de ella?" no podia funcionar;
  - no sabia que dia era hoy;
  - no habia tope de vueltas;
  - y un fallo de herramienta se convertia en un 500 opaco que el modelo nunca
    llegaba a ver, asi que tampoco podia corregirse.
"""

import json
from datetime import datetime

import pytest

import app.chatbot.nucleo as nucleo
from app.chatbot.prompt import _bloque_de_fecha


# ── El cliente falso ─────────────────────────────────────────────────────────

class _Bloque:
    def __init__(self, tipo, **kw):
        self.type = tipo
        for k, v in kw.items():
            setattr(self, k, v)


def _texto(t):
    return _Bloque('text', text=t)


def _usa(nombre, entrada=None, id='tu_1'):
    return _Bloque('tool_use', name=nombre, input=entrada or {}, id=id)


class _Respuesta:
    def __init__(self, bloques, stop_reason='end_turn'):
        self.content = bloques
        self.stop_reason = stop_reason


class _ClienteFalso:
    """Devuelve las respuestas preparadas y guarda cada peticion recibida."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.peticiones = []
        self.messages = self

    def create(self, **kwargs):
        self.peticiones.append(kwargs)
        if self.respuestas:
            return self.respuestas.pop(0)
        return _Respuesta([_texto('Fin.')])


def _montar(monkeypatch, respuestas):
    cliente = _ClienteFalso(respuestas)
    monkeypatch.setattr(nucleo, '_crear_cliente', lambda api_key: cliente)
    return cliente


# ── Historial ────────────────────────────────────────────────────────────────

def test_el_historial_llega_en_orden(db, monkeypatch):
    cliente = _montar(monkeypatch, [_Respuesta([_texto('Vale.')])])

    nucleo.chat('¿Y de ella?', 'sk-test', historial=[
        {'rol': 'user', 'texto': '¿Quién es Ana?'},
        {'rol': 'assistant', 'texto': 'Ana García, habitación 12.'},
    ])

    mensajes = cliente.peticiones[0]['messages']
    assert [m['role'] for m in mensajes] == ['user', 'assistant', 'user']
    assert mensajes[0]['content'] == '¿Quién es Ana?'
    assert mensajes[1]['content'] == 'Ana García, habitación 12.'
    assert '¿Y de ella?' in mensajes[2]['content']


def test_el_historial_solo_lleva_texto(db, monkeypatch):
    """Nunca bloques de herramienta: llevan datos de residentes en crudo.

    Y ademas, mandando solo texto es imposible construir un `tool_use` sin su
    `tool_result`, que la API rechaza con un 400.
    """
    cliente = _montar(monkeypatch, [_Respuesta([_texto('Vale.')])])
    nucleo.chat('Hola', 'sk-test', historial=[
        {'rol': 'user', 'texto': 'Hola'},
        {'rol': 'assistant', 'texto': 'Buenas'},
    ])
    for mensaje in cliente.peticiones[0]['messages']:
        assert isinstance(mensaje['content'], str)


@pytest.mark.parametrize('historial', [
    None, 'no soy una lista', [], [{'rol': 'marciano', 'texto': 'hola'}],
    [{'sin': 'forma'}], [{'rol': 'assistant', 'texto': 'empiezo yo'}],
])
def test_un_historial_raro_no_revienta(db, monkeypatch, historial):
    cliente = _montar(monkeypatch, [_Respuesta([_texto('Vale.')])])
    assert nucleo.chat('Hola', 'sk-test', historial=historial) == 'Vale.'
    # Lo que no valga se descarta; siempre queda al menos la pregunta.
    mensajes = cliente.peticiones[0]['messages']
    assert mensajes[-1]['role'] == 'user'
    assert all(m['role'] in ('user', 'assistant') for m in mensajes)


def test_el_historial_se_recorta(db, monkeypatch):
    cliente = _montar(monkeypatch, [_Respuesta([_texto('Vale.')])])
    largo = [{'rol': 'user' if i % 2 == 0 else 'assistant', 'texto': f'turno {i}'}
             for i in range(60)]
    nucleo.chat('Hola', 'sk-test', historial=largo)

    mensajes = cliente.peticiones[0]['messages']
    assert len(mensajes) <= nucleo.MAX_TURNOS_HISTORIAL + 1
    assert mensajes[0]['role'] == 'user', 'una conversacion empieza por quien pregunta'


def test_un_turno_kilometrico_se_corta(db, monkeypatch):
    cliente = _montar(monkeypatch, [_Respuesta([_texto('Vale.')])])
    nucleo.chat('Hola', 'sk-test',
                historial=[{'rol': 'user', 'texto': 'x' * 100000}])
    primero = cliente.peticiones[0]['messages'][0]['content']
    assert len(primero) <= nucleo.MAX_CARACTERES_TURNO


# ── La fecha ─────────────────────────────────────────────────────────────────

def test_la_pregunta_lleva_la_fecha_de_hoy(db, monkeypatch):
    cliente = _montar(monkeypatch, [_Respuesta([_texto('Vale.')])])
    nucleo.chat('¿Qué pasó el martes pasado?', 'sk-test')

    ultimo = cliente.peticiones[0]['messages'][-1]['content']
    assert datetime.now().date().isoformat() in ultimo
    assert 'el martes pasado fue' in ultimo


def test_el_prompt_de_sistema_no_lleva_la_fecha(db, monkeypatch):
    """Si la llevara, sus bytes cambiarian cada dia y no se podria cachear."""
    cliente = _montar(monkeypatch, [_Respuesta([_texto('Vale.')])])
    nucleo.chat('Hola', 'sk-test')

    prompt = cliente.peticiones[0]['system']
    assert datetime.now().date().isoformat() not in prompt
    assert 'Hoy es' not in prompt


def test_el_bloque_de_fecha_resuelve_los_dias_con_nombre():
    bloque = _bloque_de_fecha(datetime(2026, 10, 27, 9, 14))   # un martes
    assert '2026-10-27' in bloque
    assert 'martes 27 de octubre' in bloque
    assert 'ayer fue 2026-10-26' in bloque
    assert 'esta semana empezo el lunes 2026-10-26' in bloque
    assert 'el martes pasado fue 2026-10-20' in bloque


# ── El bucle ─────────────────────────────────────────────────────────────────

def test_ejecuta_la_herramienta_y_responde(db, monkeypatch):
    cliente = _montar(monkeypatch, [
        _Respuesta([_usa('tipos_de_atencion')], stop_reason='tool_use'),
        _Respuesta([_texto('Hay tres tipos.')]),
    ])

    assert nucleo.chat('¿Qué tipos hay?', 'sk-test') == 'Hay tres tipos.'
    assert len(cliente.peticiones) == 2
    resultados = cliente.peticiones[1]['messages'][-1]['content']
    assert resultados[0]['type'] == 'tool_result'
    assert 'is_error' not in resultados[0]


def test_el_tope_de_vueltas_corta_y_responde(db, monkeypatch):
    """Un modelo que pide herramientas sin parar no puede colgar la peticion."""
    # Justo las vueltas permitidas pidiendo herramienta; la llamada final sin
    # herramientas agota la lista y el cliente falso devuelve ya texto.
    cliente = _montar(monkeypatch, [
        _Respuesta([_usa('tipos_de_atencion', id=f't{i}')], stop_reason='tool_use')
        for i in range(nucleo.MAX_VUELTAS)
    ])

    respuesta = nucleo.chat('Dame vueltas', 'sk-test')

    assert len(cliente.peticiones) == nucleo.MAX_VUELTAS + 1
    # La ultima va SIN herramientas: es lo que le obliga a contestar.
    assert 'tools' not in cliente.peticiones[-1]
    assert respuesta == 'Fin.'


def test_varias_herramientas_a_la_vez_devuelven_un_resultado_cada_una(db, monkeypatch):
    cliente = _montar(monkeypatch, [
        _Respuesta([_usa('tipos_de_atencion', id='a'),
                    _usa('no_existe', id='b'),
                    _usa('atenciones_hoy', id='c')], stop_reason='tool_use'),
        _Respuesta([_texto('Listo.')]),
    ])

    nucleo.chat('Tres cosas', 'sk-test')

    resultados = cliente.peticiones[1]['messages'][-1]['content']
    assert len(resultados) == 3, 'dejarse uno es un 400'
    assert [r['tool_use_id'] for r in resultados] == ['a', 'b', 'c']


# ── Errores de herramienta ───────────────────────────────────────────────────

def test_una_herramienta_que_no_existe_no_tumba_la_peticion(db, monkeypatch):
    cliente = _montar(monkeypatch, [
        _Respuesta([_usa('herramienta_inventada')], stop_reason='tool_use'),
        _Respuesta([_texto('Perdona, me he equivocado.')]),
    ])

    assert nucleo.chat('Haz algo', 'sk-test') == 'Perdona, me he equivocado.'
    resultado = cliente.peticiones[1]['messages'][-1]['content'][0]
    assert resultado['is_error'] is True
    assert 'disponibles' in json.loads(resultado['content'])


def test_falta_un_parametro_y_el_modelo_se_entera(db, monkeypatch):
    cliente = _montar(monkeypatch, [
        _Respuesta([_usa('buscar_residente', {})], stop_reason='tool_use'),
        _Respuesta([_texto('Dime el nombre.')]),
    ])

    nucleo.chat('Busca', 'sk-test')

    resultado = cliente.peticiones[1]['messages'][-1]['content'][0]
    assert resultado['is_error'] is True
    assert "'nombre'" in json.loads(resultado['content'])['error']


def test_una_fecha_inventada_devuelve_un_error_util(db, monkeypatch):
    cliente = _montar(monkeypatch, [
        _Respuesta([_usa('resumen_dia', {'fecha': '2026-13-45'})], stop_reason='tool_use'),
        _Respuesta([_texto('Reformulo.')]),
    ])

    nucleo.chat('Resumen', 'sk-test')

    resultado = cliente.peticiones[1]['messages'][-1]['content'][0]
    assert resultado['is_error'] is True
    assert 'AAAA-MM-DD' in json.loads(resultado['content'])['error']


def test_una_herramienta_que_revienta_no_envenena_a_las_siguientes(db, monkeypatch):
    """Sin rollback, la sesion rota hacia fallar todo lo que viniera despues."""
    def _explota(args):
        raise RuntimeError('boom')

    monkeypatch.setitem(nucleo._get_tool_handlers.__globals__, '_X', None)
    handlers_reales = nucleo._get_tool_handlers

    def _con_bomba(is_admin=False):
        h = handlers_reales(is_admin)
        h['tipos_de_atencion'] = _explota
        return h

    monkeypatch.setattr(nucleo, '_get_tool_handlers', _con_bomba)
    cliente = _montar(monkeypatch, [
        _Respuesta([_usa('tipos_de_atencion', id='a'),
                    _usa('atenciones_hoy', id='b')], stop_reason='tool_use'),
        _Respuesta([_texto('Listo.')]),
    ])

    assert nucleo.chat('Dos cosas', 'sk-test') == 'Listo.'
    resultados = cliente.peticiones[1]['messages'][-1]['content']
    assert resultados[0]['is_error'] is True
    # La segunda se ejecuta igual y devuelve datos de verdad.
    assert 'is_error' not in resultados[1]


# ── La respuesta final ───────────────────────────────────────────────────────

def test_una_negativa_del_modelo_se_explica(db, monkeypatch):
    _montar(monkeypatch, [_Respuesta([], stop_reason='refusal')])
    respuesta = nucleo.chat('...', 'sk-test')
    assert 'No puedo responder' in respuesta


def test_los_bloques_que_no_son_texto_no_se_cuelan(db, monkeypatch):
    _montar(monkeypatch, [_Respuesta([
        _Bloque('thinking', thinking='...'), _texto('La respuesta.')])])
    assert nucleo.chat('Hola', 'sk-test') == 'La respuesta.'


def test_el_cliente_se_crea_con_timeout():
    """Sin timeout, una llamada lenta deja colgado un worker de gunicorn."""
    cliente = nucleo._crear_cliente('sk-test')
    assert cliente.timeout == nucleo.TIMEOUT_API
