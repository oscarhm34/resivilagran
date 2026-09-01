"""
test_redirecciones.py — Ninguna redireccion se fia del Referer.

Varias rutas del panel devuelven al administrador a donde estaba pulsando, y
para eso leen la cabecera `Referer`. Esa cabecera la pone quien enlaza a la
pagina, no el servidor: aceptarla tal cual convierte un formulario del panel en
un salto a otra web, que es la mitad del trabajo de una pagina de phishing.

La regla del proyecto ya lo decia para el `next` del login; esto lo extiende a
todas las vueltas atras.
"""

import pytest

from app.utils import volver_atras


RUTAS = [
    ('/admin/close-session/cleaning/1', '/registros-limpieza'),
    ('/admin/close-session/care/1', '/registros-atencion'),
]

TRAMPAS = [
    'https://sitio-malicioso.example/trampa',
    '//sitio-malicioso.example/trampa',
    'http://sitio-malicioso.example',
]


@pytest.mark.parametrize('ruta,destino', RUTAS)
@pytest.mark.parametrize('trampa', TRAMPAS)
def test_las_rutas_del_panel_no_saltan_a_otra_web(auth_client, db, ruta, destino, trampa):
    res = auth_client.post(ruta, headers={'Referer': trampa})

    assert res.status_code == 302
    assert 'sitio-malicioso' not in res.headers['Location']
    assert res.headers['Location'].endswith(destino)


@pytest.mark.parametrize('trampa', TRAMPAS)
def test_el_helper_descarta_cualquier_destino_externo(app, trampa):
    with app.test_request_context('/', headers={'Referer': trampa}):
        assert volver_atras('/por-defecto') == '/por-defecto'


def test_el_helper_respeta_una_ruta_propia(app):
    with app.test_request_context('/', headers={'Referer': 'http://localhost/registros-atencion?estado=abierta'}):
        assert volver_atras('/por-defecto') == '/registros-atencion?estado=abierta'


def test_sin_referer_se_usa_el_destino_por_defecto(app):
    with app.test_request_context('/'):
        assert volver_atras('/por-defecto') == '/por-defecto'
