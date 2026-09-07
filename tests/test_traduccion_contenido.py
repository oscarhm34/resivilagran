"""
test_traduccion_contenido.py — Lo que escribe coordinacion, en el idioma de
quien lo tiene que leer.

Las instrucciones de un tipo de atencion son texto de seguridad: dicen que hay
que hacer y como mientras se atiende a una persona mayor. Hasta ahora salian
siempre en castellano, tambien para quien no lo lee.

Lo que mas importa comprobar aqui no es que traduzca, sino la regla que protege
de una traduccion mala: si alguien cambia el original en castellano, la
traduccion vieja **deja de servirse** y se vuelve al original. Una instruccion
que ya no dice lo que dice la de castellano es peor que no tener traduccion.
"""

from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import (CareType, ChecklistItem, Cleaner, ContentTranslation,
                        Resident, CleaningRecord)
from app.utils import _huella, _texto_traducido, CAMPOS_TRADUCIBLES


ORIGINAL = 'Levantar de la cama con la grua. Nunca sola.'
EN_ARABE = 'ارفع المقيم من السرير بالرافعة. لا تفعلي ذلك وحدك أبدًا.'


@pytest.fixture
def trabajadora_arabe(db, cleaner_user):
    cleaner_user.lang = 'ar'
    db.session.commit()
    return cleaner_user


@pytest.fixture
def headers_arabe(db, trabajadora_arabe, app):
    with app.app_context():
        token = create_access_token(identity=trabajadora_arabe.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def headers_castellano(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def tipo_atencion(db):
    ct = CareType(name='Acostar', instructions=ORIGINAL, active=True)
    db.session.add(ct)
    db.session.commit()
    return ct


def _traducir(db, entity_type, entity_id, field, lang, texto, original,
              revisada=True):
    t = ContentTranslation(entity_type=entity_type, entity_id=entity_id,
                           field=field, lang=lang, text=texto,
                           source_hash=_huella(original), reviewed=revisada,
                           generated_at=datetime.now())
    db.session.add(t)
    db.session.commit()
    return t


# ── Se sirve en el idioma de la ficha ────────────────────────────────────────

def test_las_instrucciones_llegan_en_arabe(client, db, tipo_atencion, headers_arabe):
    _traducir(db, 'care_type', tipo_atencion.id, 'instructions', 'ar',
              EN_ARABE, ORIGINAL)

    datos = client.get('/api/care-types', headers=headers_arabe).get_json()

    assert datos[0]['instructions'] == EN_ARABE


def test_en_castellano_no_cambia_nada(client, db, tipo_atencion, headers_castellano):
    _traducir(db, 'care_type', tipo_atencion.id, 'instructions', 'ar',
              EN_ARABE, ORIGINAL)

    datos = client.get('/api/care-types', headers=headers_castellano).get_json()

    assert datos[0]['instructions'] == ORIGINAL


def test_sin_traduccion_se_sirve_el_castellano(client, db, tipo_atencion,
                                               headers_arabe):
    """Sin traduccion no se deja a nadie con la pantalla en blanco."""
    datos = client.get('/api/care-types', headers=headers_arabe).get_json()

    assert datos[0]['instructions'] == ORIGINAL


def test_el_idioma_sale_del_token_y_no_del_cliente(client, db, tipo_atencion,
                                                   headers_castellano):
    """El idioma es de la persona, no de la peticion: no hay nada que falsear."""
    _traducir(db, 'care_type', tipo_atencion.id, 'instructions', 'ar',
              EN_ARABE, ORIGINAL)

    datos = client.get('/api/care-types?lang=ar',
                       headers=headers_castellano).get_json()

    assert datos[0]['instructions'] == ORIGINAL


# ── La regla que importa: una traduccion desfasada no se sirve ───────────────

def test_si_cambia_el_castellano_la_traduccion_deja_de_servirse(
        client, db, tipo_atencion, headers_arabe):
    _traducir(db, 'care_type', tipo_atencion.id, 'instructions', 'ar',
              EN_ARABE, ORIGINAL)

    tipo_atencion.instructions = 'Levantar de la cama con la grua. Siempre entre dos.'
    db.session.commit()

    datos = client.get('/api/care-types', headers=headers_arabe).get_json()

    assert datos[0]['instructions'] == 'Levantar de la cama con la grua. Siempre entre dos.'


def test_al_rehacerla_vuelve_a_servirse(client, db, tipo_atencion, headers_arabe):
    _traducir(db, 'care_type', tipo_atencion.id, 'instructions', 'ar',
              EN_ARABE, ORIGINAL)
    nuevo = 'Levantar de la cama con la grua. Siempre entre dos.'
    tipo_atencion.instructions = nuevo
    db.session.commit()

    fila = ContentTranslation.query.one()
    fila.text = 'نص جديد'
    fila.source_hash = _huella(nuevo)
    db.session.commit()

    datos = client.get('/api/care-types', headers=headers_arabe).get_json()

    assert datos[0]['instructions'] == 'نص جديد'


def test_los_espacios_de_sobra_no_desfasan_la_traduccion(db, tipo_atencion):
    """Guardar el formulario con un espacio de mas no puede invalidarla."""
    _traducir(db, 'care_type', tipo_atencion.id, 'instructions', 'ar',
              EN_ARABE, ORIGINAL)

    resultado = _texto_traducido('care_type', tipo_atencion.id, 'instructions',
                                 '  ' + ORIGINAL + '  ', 'ar')

    assert resultado == EN_ARABE


# ── El checklist y la informacion del residente ──────────────────────────────

def test_el_checklist_sale_traducido(client, db, cleaner_user, room, room_type,
                                     headers_arabe):
    item = ChecklistItem(text='Apagar la luz', active=True,
                         room_types=[room_type])
    db.session.add(item)
    db.session.add(CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                                  start_time=datetime.now() - timedelta(minutes=10)))
    db.session.commit()
    _traducir(db, 'checklist_item', item.id, 'text', 'ar', 'أطفئ الضوء',
              'Apagar la luz')

    res = client.post('/api/nfc/scan', headers=headers_arabe,
                      json={'nfc_code': room.number, 'mode': 'cleaning',
                            'worker_id': cleaner_user.id})

    assert [i['text'] for i in res.get_json()['items']] == ['أطفئ الضوء']


def test_la_informacion_del_residente_sale_traducida(client, db, headers_arabe):
    r = Resident(name='Josefa Ruiz', nfc_code='RES-TRAD-1', active=True,
                 relevant_info='Es sorda del oido derecho.')
    db.session.add(r)
    db.session.commit()
    _traducir(db, 'resident', r.id, 'relevant_info', 'ar',
              'صمّاء في الأذن اليمنى.', 'Es sorda del oido derecho.')

    datos = client.get(f'/api/resident/{r.id}/info', headers=headers_arabe).get_json()

    assert datos['relevant_info'] == 'صمّاء في الأذن اليمنى.'
    assert datos['name'] == 'Josefa Ruiz'      # los nombres no se traducen


# ── Panel de administracion ──────────────────────────────────────────────────

def test_la_pantalla_pide_administrador(client, db, tipo_atencion):
    res = client.get('/traducciones')

    assert res.status_code in (302, 401, 403)


def test_guardar_una_traduccion_a_mano_la_marca_revisada(auth_client, db,
                                                         tipo_atencion):
    res = auth_client.post('/api/traducciones/guardar', json={
        'entity_type': 'care_type', 'entity_id': tipo_atencion.id,
        'field': 'instructions', 'lang': 'ar', 'text': EN_ARABE,
    })

    assert res.status_code == 200
    fila = ContentTranslation.query.one()
    assert fila.reviewed is True
    assert fila.source_hash == _huella(ORIGINAL)


def test_no_se_puede_guardar_un_campo_que_no_es_traducible(auth_client, db,
                                                           tipo_atencion):
    """La lista de campos manda: si no, cualquier columna seria escribible."""
    res = auth_client.post('/api/traducciones/guardar', json={
        'entity_type': 'care_type', 'entity_id': tipo_atencion.id,
        'field': 'icon', 'lang': 'ar', 'text': 'x',
    })

    assert res.status_code == 404
    assert ContentTranslation.query.count() == 0


def test_un_idioma_que_no_hablamos_se_rechaza(auth_client, db, tipo_atencion):
    res = auth_client.post('/api/traducciones/guardar', json={
        'entity_type': 'care_type', 'entity_id': tipo_atencion.id,
        'field': 'instructions', 'lang': 'de', 'text': 'x',
    })

    assert res.status_code == 400
    assert ContentTranslation.query.count() == 0


def test_no_se_puede_guardar_el_castellano(auth_client, db, tipo_atencion):
    """El castellano es el original: guardarlo aqui seria duplicarlo."""
    res = auth_client.post('/api/traducciones/guardar', json={
        'entity_type': 'care_type', 'entity_id': tipo_atencion.id,
        'field': 'instructions', 'lang': 'es', 'text': 'x',
    })

    assert res.status_code == 400


def test_una_traduccion_vacia_se_rechaza(auth_client, db, tipo_atencion):
    res = auth_client.post('/api/traducciones/guardar', json={
        'entity_type': 'care_type', 'entity_id': tipo_atencion.id,
        'field': 'instructions', 'lang': 'ar', 'text': '   ',
    })

    assert res.status_code == 400
    assert ContentTranslation.query.count() == 0


def test_borrar_deja_el_texto_en_castellano(auth_client, db, tipo_atencion,
                                            client, headers_arabe):
    _traducir(db, 'care_type', tipo_atencion.id, 'instructions', 'ar',
              EN_ARABE, ORIGINAL)

    auth_client.post('/api/traducciones/borrar', json={
        'entity_type': 'care_type', 'entity_id': tipo_atencion.id,
        'field': 'instructions', 'lang': 'ar',
    })

    assert ContentTranslation.query.count() == 0
    datos = client.get('/api/care-types', headers=headers_arabe).get_json()
    assert datos[0]['instructions'] == ORIGINAL


def test_la_pantalla_lista_lo_que_hay_escrito(auth_client, db, tipo_atencion):
    html = auth_client.get('/traducciones').get_data(as_text=True)

    assert 'Acostar' in html
    assert ORIGINAL in html


def test_la_pantalla_avisa_de_las_desfasadas(auth_client, db, tipo_atencion):
    _traducir(db, 'care_type', tipo_atencion.id, 'instructions', 'ar',
              EN_ARABE, 'un texto que ya no es el que hay')

    html = auth_client.get('/traducciones').get_data(as_text=True)

    assert 'Desfasada' in html


# ── La lista de campos ───────────────────────────────────────────────────────

def test_no_se_traduce_nada_que_no_haga_falta():
    """Un numero de habitacion o el nombre de un grupo no gana nada traducido."""
    assert set(CAMPOS_TRADUCIBLES) == {'care_type', 'checklist_item', 'resident'}
    assert 'name' not in CAMPOS_TRADUCIBLES['resident']
