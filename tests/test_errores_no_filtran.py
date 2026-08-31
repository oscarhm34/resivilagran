"""
test_errores_no_filtran.py — Las respuestas de error no exponen detalle interno.

La regla 04-seguridad prohíbe volcar datos de residentes en mensajes de error, y
la 01-backend-flask exige que el detalle técnico vaya al log, no al cliente.
Catorce rutas devolvían `str(e)`, y un `IntegrityError` de SQLAlchemy arrastra la
sentencia SQL **con sus parámetros**: nombres, valores clínicos, rutas de fotos.

Estos tests fuerzan el fallo y comprueban las dos mitades: que el cliente recibe
un mensaje en castellano sin detalle, y que el log sí conserva la causa.
"""

import pytest

from app.models import Resident


# Rastros de que se ha colado un error interno en la respuesta.
RASTROS = ('Traceback', 'SELECT ', 'INSERT ', 'sqlalchemy', 'psycopg2',
           'sqlite3', 'IntegrityError', 'NoneType', 'KeyError')


@pytest.fixture
def residente(db):
    r = Resident(name='Josefa Ruiz', nfc_code='RES-ERR-1', active=True,
                 allergies='Penicilina')
    db.session.add(r)
    db.session.commit()
    return r


def test_el_watchlist_no_devuelve_el_error_interno(auth_client, db, residente, monkeypatch):
    """La lista de vigilancia recorre residentes: el error no puede llevar sus datos."""
    from app.blueprints import assessments

    def revienta(*args, **kwargs):
        raise RuntimeError('SELECT resident.name FROM resident -- Josefa Ruiz, Penicilina')

    monkeypatch.setattr(assessments, '_call_claude', revienta)

    response = auth_client.post('/api/ai/risk-watchlist', json={})
    cuerpo = response.get_data(as_text=True)

    assert response.status_code == 500
    assert 'Josefa Ruiz' not in cuerpo
    assert 'Penicilina' not in cuerpo
    for rastro in RASTROS:
        assert rastro not in cuerpo
    assert 'No se ha podido' in response.get_json()['error']


def test_el_detalle_del_error_si_llega_al_log(auth_client, db, residente, monkeypatch, caplog):
    """Sin log no habría forma de diagnosticar nada: el detalle tiene que quedar."""
    from app.blueprints import assessments

    def revienta(*args, **kwargs):
        raise RuntimeError('la causa concreta del fallo')

    monkeypatch.setattr(assessments, '_call_claude', revienta)

    with caplog.at_level('ERROR'):
        auth_client.post('/api/ai/risk-watchlist', json={})

    assert 'la causa concreta del fallo' in caplog.text


def test_ninguna_ruta_devuelve_str_de_la_excepcion():
    """Barrido estático: que no vuelva a colarse el patrón en un blueprint nuevo.

    Las dos excepciones son los `except ValueError` de nfc.py, que propagan
    mensajes escritos a mano en castellano ('Imagen base64 no válida.').
    """
    import pathlib

    culpables = []
    for ruta in pathlib.Path('app/blueprints').glob('*.py'):
        lineas = ruta.read_text(encoding='utf-8').split('\n')
        for i, linea in enumerate(lineas, 1):
            if 'str(e)' not in linea or 'jsonify' not in linea:
                continue
            anterior = lineas[i - 2] if i >= 2 else ''
            if 'except ValueError' in anterior:
                continue
            culpables.append(f'{ruta.name}:{i}')

    assert culpables == [], f'Devuelven el error interno al cliente: {culpables}'
