"""
test_smoke_routes.py — Barrido de todas las rutas GET del panel admin.

Dos controles sistemáticos que con 230 rutas y protección manual no se pueden
hacer a mano:

1. Ninguna ruta del admin queda accesible sin sesión.
2. Ninguna ruta del admin devuelve 500 con la base de datos vacía.

Se usa el test client de Flask (sin navegador ni servidor) sobre la BD temporal
de conftest.py. Solo se recorren rutas GET sin parámetros: las que escriben o
borran se prueban una a una en sus ficheros de test.
"""

import importlib.util

import pytest

from app import app as flask_app


def _falta(modulo):
    return importlib.util.find_spec(modulo) is None


# Rutas públicas por diseño: login, shell de la PWA y ficheros de la PWA.
RUTAS_PUBLICAS = {
    'admin_bp.admin_login',
    'nfc.login',
    'nfc.worker',
    'nfc.service_worker',
    'nfc.worker_manifest',
    # Comprueba current_user.is_authenticated en el cuerpo y devuelve 0 si no
    # hay sesión, así que no filtra nada.
    'notifications.unread_count',
    'static',
}

# Excluidas del barrido de 500: llaman a servicios externos o generan ficheros
# pesados, y su fallo no diría nada sobre el estado del panel.
RUTAS_EXCLUIDAS_DEL_BARRIDO = RUTAS_PUBLICAS | {
    'chat.chat_page',
}

# Los exports a Excel dependen de motores opcionales de pandas. Están en
# requirements.txt, pero si el entorno local no los tiene el fallo es del
# entorno, no del código: se salta en vez de dar un falso positivo.
if _falta('openpyxl'):
    RUTAS_EXCLUIDAS_DEL_BARRIDO |= {'activities.export_activity_stats'}
if _falta('xlsxwriter'):
    RUTAS_EXCLUIDAS_DEL_BARRIDO |= {
        'admin_bp.exportar_excel',
        'residents.exportar_fichajes',
        'residents.exportar_atenciones_excel',
    }


def _rutas_get(excluidas):
    """Reglas GET sin parámetros dinámicos, ordenadas para un id estable."""
    vistas = []
    for rule in flask_app.url_map.iter_rules():
        if 'GET' not in (rule.methods or set()):
            continue
        if rule.endpoint in excluidas or rule.arguments:
            continue
        vistas.append(rule.rule)
    return [pytest.param(r, id=r) for r in sorted(set(vistas))]


RUTAS_PROTEGIDAS = _rutas_get(RUTAS_PUBLICAS)
RUTAS_BARRIDO = _rutas_get(RUTAS_EXCLUIDAS_DEL_BARRIDO)


def test_el_barrido_cubre_rutas():
    """Guarda contra un filtro que se quede sin nada que recorrer."""
    assert len(RUTAS_BARRIDO) > 30


@pytest.mark.parametrize('path', RUTAS_PROTEGIDAS)
def test_ruta_get_no_es_accesible_sin_sesion(client, path):
    resp = client.get(path)

    assert resp.status_code in (302, 401, 403), (
        f'{path} responde {resp.status_code} sin autenticación'
    )
    if resp.status_code == 302:
        assert '/login' in resp.headers.get('Location', '')


@pytest.mark.parametrize('path', RUTAS_BARRIDO)
def test_ruta_get_no_revienta_con_bd_vacia(auth_client, path):
    """Detecta los 500 por datos ausentes, parámetros sin default o plantillas rotas."""
    resp = auth_client.get(path)

    assert resp.status_code < 500, f'{path} devuelve {resp.status_code}'
