"""
test_idiomas.py — La webapp habla castellano, arabe, frances e ingles.

En la residencia trabaja gente que no lee castellano con soltura, y hasta ahora
la webapp solo hablaba castellano. El idioma va en el perfil y no solo en el
movil, para que siga a la trabajadora si cambia de telefono y para que
coordinacion se lo pueda poner desde el panel.

La traduccion en si se aplica en el navegador —el castellano es la clave y un
MutationObserver la sustituye sobre el DOM—, asi que lo que se comprueba aqui es
el lado del servidor: que el idioma se guarda, que solo lo cambia su duena, que
llega a la webapp y que la tabla de traducciones esta completa y bien formada.
"""

import io
import json
import os

import pytest
from flask_jwt_extended import create_access_token

from app.models import Cleaner
from app.utils import APP_LANGUAGES, APP_LOCALES, _idioma_valido, _traducciones_webapp


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def otra_trabajadora(db):
    c = Cleaner(username='fatima', name='Fatima', role='limpieza')
    c.set_password('x')
    db.session.add(c)
    db.session.commit()
    return c


# ── Guardar el idioma ────────────────────────────────────────────────────────

def test_guarda_el_idioma_en_el_perfil(client, db, cleaner_user, worker_headers):
    res = client.put('/api/worker/lang', headers=worker_headers, json={'lang': 'ar'})

    assert res.status_code == 200
    assert res.get_json()['lang'] == 'ar'
    assert db.session.get(Cleaner, cleaner_user.id).lang == 'ar'


def test_un_idioma_que_no_hablamos_se_rechaza(client, db, cleaner_user, worker_headers):
    res = client.put('/api/worker/lang', headers=worker_headers, json={'lang': 'de'})

    assert res.status_code == 400
    assert res.get_json()['code'] == 'LANG_UNKNOWN'
    assert db.session.get(Cleaner, cleaner_user.id).lang is None


def test_sin_token_no_se_puede_cambiar(client, db, cleaner_user):
    res = client.put('/api/worker/lang', json={'lang': 'fr'})

    assert res.status_code == 401
    assert db.session.get(Cleaner, cleaner_user.id).lang is None


def test_solo_cambia_el_idioma_de_quien_manda_el_token(
        client, db, cleaner_user, otra_trabajadora, worker_headers):
    """La identidad sale del token: no hay ningun id que se pueda suplantar."""
    client.put('/api/worker/lang', headers=worker_headers,
               json={'lang': 'fr', 'worker_id': otra_trabajadora.id})

    assert db.session.get(Cleaner, cleaner_user.id).lang == 'fr'
    assert db.session.get(Cleaner, otra_trabajadora.id).lang is None


# ── Que el idioma llegue a la webapp ─────────────────────────────────────────

def test_el_login_devuelve_el_idioma(client, db, cleaner_user):
    cleaner_user.lang = 'fr'
    db.session.commit()

    res = client.post('/login', json={'username': cleaner_user.username,
                                      'password': 'limpia123'})

    assert res.status_code == 200
    assert res.get_json()['lang'] == 'fr'


def test_sin_idioma_elegido_el_login_devuelve_castellano(client, db, cleaner_user):
    res = client.post('/login', json={'username': cleaner_user.username,
                                      'password': 'limpia123'})

    assert res.get_json()['lang'] == 'es'


def test_la_configuracion_lo_devuelve_para_los_cambios_desde_el_panel(
        client, db, cleaner_user, worker_headers):
    """Sin esto, cambiarlo desde Empleados no llegaria hasta el siguiente login."""
    cleaner_user.lang = 'en'
    db.session.commit()

    res = client.get('/api/config', headers=worker_headers)

    assert res.get_json()['lang'] == 'en'


def test_la_lista_de_idiomas_trae_lo_que_necesita_el_selector(client, worker_headers):
    res = client.get('/api/worker/languages', headers=worker_headers)

    codigos = {l['code'] for l in res.get_json()['languages']}
    assert codigos == {'es', 'ar', 'fr', 'en'}
    arabe = next(l for l in res.get_json()['languages'] if l['code'] == 'ar')
    assert arabe['rtl'] is True
    assert arabe['locale'] == 'ar-MA'


# ── La tabla de traducciones ─────────────────────────────────────────────────

def test_los_tres_idiomas_traducen_las_mismas_claves():
    """Una clave suelta en un idioma es un texto que sale en castellano."""
    tabla = _traducciones_webapp()

    assert set(tabla) == {'ar', 'fr', 'en'}
    claves = {idioma: set(t) for idioma, t in tabla.items()}
    assert claves['ar'] == claves['fr'] == claves['en']


def test_ninguna_traduccion_esta_vacia():
    for idioma, textos in _traducciones_webapp().items():
        vacias = [k for k, v in textos.items() if not v.strip()]
        assert not vacias, f'{idioma}: {vacias[:5]}'


def test_los_huecos_de_los_textos_con_datos_se_conservan():
    """Si la traduccion pierde un {0}, el dato desaparece de la pantalla."""
    import re
    for idioma, textos in _traducciones_webapp().items():
        for clave, trad in textos.items():
            esperados = set(re.findall(r'\{\d\}', clave))
            assert set(re.findall(r'\{\d\}', trad)) == esperados, \
                f'{idioma}: {clave!r} -> {trad!r}'


def test_el_castellano_no_esta_en_la_tabla():
    """El castellano es la clave: tenerlo tambien como valor seria duplicarlo."""
    assert 'es' not in _traducciones_webapp()


def test_el_fichero_de_traducciones_es_json_valido():
    ruta = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        'app', 'i18n', 'worker.json')
    with io.open(ruta, encoding='utf-8') as fh:
        datos = json.load(fh)
    assert datos['ar']['Cerrar sesión']
    assert datos['en']['Cerrar sesión'] == 'Sign out'


# ── Normalizacion ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('entrada,esperado', [
    ('ar', 'ar'), ('fr', 'fr'), ('en', 'en'), ('es', 'es'),
    (None, 'es'), ('', 'es'), ('de', 'es'), ('ca', 'es'), ('AR', 'es'),
])
def test_un_idioma_desconocido_cae_a_castellano(entrada, esperado):
    assert _idioma_valido(entrada) == esperado


def test_cada_idioma_tiene_su_locale():
    """`toLocaleDateString` y el dictado por voz necesitan el codigo completo."""
    assert set(APP_LOCALES) == set(APP_LANGUAGES)
    assert all(APP_LOCALES[c] for c in APP_LANGUAGES)


def test_solo_el_arabe_va_de_derecha_a_izquierda():
    rtl = {c for c, d in APP_LANGUAGES.items() if d['rtl']}
    assert rtl == {'ar'}


# ── Panel de administracion ──────────────────────────────────────────────────

def test_coordinacion_puede_ponerle_el_idioma_a_una_trabajadora(
        auth_client, db, otra_trabajadora):
    auth_client.post('/cleaners/add_edit', data={
        'cleaner_id': otra_trabajadora.id, 'username': 'fatima',
        'name': 'Fatima', 'role': 'limpieza', 'active': '1', 'lang': 'ar',
    }, follow_redirects=True)

    assert db.session.get(Cleaner, otra_trabajadora.id).lang == 'ar'


def test_el_panel_rechaza_un_idioma_que_no_hablamos(auth_client, db, otra_trabajadora):
    otra_trabajadora.lang = 'fr'
    db.session.commit()

    auth_client.post('/cleaners/add_edit', data={
        'cleaner_id': otra_trabajadora.id, 'username': 'fatima',
        'name': 'Fatima', 'role': 'limpieza', 'active': '1', 'lang': 'de',
    }, follow_redirects=True)

    assert db.session.get(Cleaner, otra_trabajadora.id).lang is None


def test_la_webapp_lleva_la_tabla_dentro(client):
    """Va incrustada y no en /static: alli el service worker la cachearia."""
    html = client.get('/worker').get_data(as_text=True)

    assert 'var TABLA = {' in html
    assert '"Cerrar sesión": "Sign out"' in html
