"""
test_chatbot_guias.py — El asistente explicando como se hacen las cosas.

Sabia consultar datos pero no explicar procedimientos: a "como se hace una
cura?" miraba la base de datos, que no es la pregunta.

El procedimiento vive en `app/chatbot/guias.py` y tambien en `admin_help.html`,
porque raspar HTML en caliente se rompe en silencio. El precio es que pueden
separarse, asi que aqui se comprueba que las dos versiones siguen diciendo lo
mismo en lo que importa.
"""

import io
import json
import os

import pytest

from app.chatbot import TOOLS, _get_tool_handlers
from app.chatbot.guias import GUIAS, TEMAS
from app.chatbot.prompt import SYSTEM_PROMPT


AYUDA = os.path.join(os.path.dirname(__file__), '..', 'app', 'templates',
                     'admin_help.html')


@pytest.fixture
def explicar(db):
    return _get_tool_handlers(False)['como_funciona']


# ── La herramienta ───────────────────────────────────────────────────────────

def test_explica_el_procedimiento_de_las_curas(explicar):
    datos = json.loads(explicar({'tema': 'curas'}))
    assert datos['tema'] == 'curas'
    assert len(datos['procedimiento']) > 1000


def test_un_tema_que_no_existe_ofrece_los_que_hay(explicar):
    """Un vacio mudo lo leeria como 'no hay procedimiento'."""
    datos = json.loads(explicar({'tema': 'la fotocopiadora'}))
    assert 'error' in datos
    assert datos['temas_disponibles'] == TEMAS


def test_el_tema_no_distingue_mayusculas(explicar):
    assert 'procedimiento' in json.loads(explicar({'tema': '  CURAS '}))


def test_esta_disponible_para_las_dos(db):
    """Un procedimiento no es dato de nadie: lo puede preguntar cualquiera."""
    assert 'como_funciona' in _get_tool_handlers(False)
    assert 'como_funciona' in _get_tool_handlers(True)


def test_la_herramienta_esta_en_el_catalogo():
    esquema = next(t for t in TOOLS if t['name'] == 'como_funciona')
    assert esquema['input_schema']['required'] == ['tema']
    # El enum tiene que seguir a las guias, o el modelo pedira temas que no hay.
    assert sorted(esquema['input_schema']['properties']['tema']['enum']) == TEMAS


def test_el_prompt_le_dice_cuando_usarla():
    assert 'como_funciona' in SYSTEM_PROMPT


# ── Que la guia y la ayuda no se separen ─────────────────────────────────────

CLAVES_CURAS = [
    'Curas abiertas',          # el apartado del movil
    'Curada',                  # el estado que cierra
    'Empeorando',              # el que dispara la alerta
    'cuatro',                  # el tope de fotos
    'auditoria',               # donde queda registrado
]


@pytest.mark.parametrize('clave', CLAVES_CURAS)
def test_la_guia_de_curas_dice_lo_mismo_que_la_ayuda(clave):
    ayuda = io.open(AYUDA, encoding='utf-8').read()
    i = ayuda.index('<!-- 16k.')
    j = ayuda.index('<!-- 17. Inteligencia')
    seccion = ayuda[i:j]

    assert clave.lower() in GUIAS['curas'].lower(), f'falta en la guia: {clave}'
    assert clave.lower() in seccion.lower(), f'falta en la ayuda: {clave}'


def test_las_dos_dicen_que_no_se_deja_la_atencion_abierta():
    """Es la confusion que mas dano hace, y tiene que estar en los dos sitios."""
    ayuda = io.open(AYUDA, encoding='utf-8').read()
    assert 'atencion abierta' in GUIAS['curas'].lower()
    assert 'atencion abierta' in ayuda.lower()


def test_todas_las_guias_tienen_contenido():
    for tema, texto in GUIAS.items():
        assert len(texto) > 500, f'la guia de {tema} se ha quedado corta'
