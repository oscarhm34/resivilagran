"""
test_belongings_busqueda.py — Busqueda en el inventario de toda la casa.

Lo que se protege aqui: que la busqueda global exija token, que filtre por tipo,
color y descripcion, que no devuelva pertenencias de residentes dados de baja y
que el tope de resultados no se salte en silencio (el movil dice cuantas hay en
total).
"""

import pytest
from flask_jwt_extended import create_access_token

from app.models import BelongingPhoto, Resident, ResidentBelonging


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def antonia(db):
    r = Resident(name='Antonia Vidal', nfc_code='RES-BUS-1', active=True,
                 room_number='101')
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def josep(db):
    r = Resident(name='Josep Roca', nfc_code='RES-BUS-2', active=True,
                 room_number='202')
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def inventario(db, antonia, josep):
    """Tres objetos repartidos entre dos residentes, con color y categoria."""
    def objeto(residente, descripcion, categoria, color):
        item = ResidentBelonging(resident_id=residente.id, description=descripcion,
                                 category=categoria)
        db.session.add(item)
        db.session.flush()
        db.session.add(BelongingPhoto(belonging_id=item.id, color=color,
                                      photo_path=f'belongings/res_{residente.id}/{descripcion}.jpg'))
        return item

    objeto(antonia, 'Jersey de lana', 'ropa', 'azul')
    objeto(antonia, 'Maquinilla electrica', 'higiene', 'negro')
    objeto(josep, 'Bufanda gruesa', 'ropa', 'rojo')
    db.session.commit()


def _descripciones(res):
    return sorted(b['description'] for b in res.get_json()['belongings'])


# ── Autorizacion ──────────────────────────────────────────────────────────────

def test_sin_token_no_se_busca(client, db):
    assert client.get('/api/worker/belongings/search').status_code == 401


# ── Filtros ───────────────────────────────────────────────────────────────────

def test_sin_filtros_devuelve_todo_el_inventario(client, db, inventario, worker_headers):
    res = client.get('/api/worker/belongings/search', headers=worker_headers)

    assert res.status_code == 200
    assert _descripciones(res) == ['Bufanda gruesa', 'Jersey de lana',
                                   'Maquinilla electrica']


def test_filtro_por_categoria(client, db, inventario, worker_headers):
    res = client.get('/api/worker/belongings/search?category=ropa',
                     headers=worker_headers)

    assert _descripciones(res) == ['Bufanda gruesa', 'Jersey de lana']


def test_filtro_por_color(client, db, inventario, worker_headers):
    res = client.get('/api/worker/belongings/search?color=azul',
                     headers=worker_headers)

    assert _descripciones(res) == ['Jersey de lana']


def test_filtro_por_texto_no_distingue_mayusculas(client, db, inventario, worker_headers):
    res = client.get('/api/worker/belongings/search?q=JERSEY', headers=worker_headers)

    assert _descripciones(res) == ['Jersey de lana']


def test_filtro_por_residente(client, db, inventario, josep, worker_headers):
    res = client.get(f'/api/worker/belongings/search?resident_id={josep.id}',
                     headers=worker_headers)

    assert _descripciones(res) == ['Bufanda gruesa']


def test_los_filtros_se_combinan(client, db, inventario, worker_headers):
    res = client.get('/api/worker/belongings/search?category=ropa&color=rojo',
                     headers=worker_headers)

    assert _descripciones(res) == ['Bufanda gruesa']


def test_un_filtro_inventado_no_filtra_nada(client, db, inventario, worker_headers):
    """Una categoria que no existe se ignora, no devuelve vacio ni revienta."""
    res = client.get('/api/worker/belongings/search?category=pirata&color=turquesa',
                     headers=worker_headers)

    assert res.status_code == 200
    assert len(res.get_json()['belongings']) == 3


# ── Que sale en cada fila ─────────────────────────────────────────────────────

def test_cada_objeto_dice_de_quien_es_y_en_que_habitacion(
        client, db, inventario, antonia, worker_headers):
    """Sin el duenno la busqueda no sirve: es justo lo que se quiere saber."""
    res = client.get('/api/worker/belongings/search?q=jersey', headers=worker_headers)

    fila = res.get_json()['belongings'][0]
    assert fila['resident_name'] == 'Antonia Vidal'
    assert fila['room'] == '101'
    assert fila['resident_id'] == antonia.id


def test_devuelve_los_catalogos_para_pintar_los_filtros(client, db, inventario,
                                                        worker_headers):
    datos = client.get('/api/worker/belongings/search',
                       headers=worker_headers).get_json()

    assert {'id': 'ropa', 'label': 'Ropa'} in datos['categories']
    assert {'id': 'azul', 'label': 'Azul'} in datos['colors']


# ── Residentes de baja y tope de resultados ──────────────────────────────────

def test_un_residente_de_baja_no_sale(client, db, inventario, josep, worker_headers):
    josep.active = False
    db.session.commit()

    res = client.get('/api/worker/belongings/search', headers=worker_headers)

    assert 'Bufanda gruesa' not in _descripciones(res)


def test_el_tope_no_se_aplica_en_silencio(client, db, antonia, worker_headers):
    """Si se recorta, el movil tiene que poder decir cuantas hay en total."""
    from app.blueprints.residents import TOPE_BUSQUEDA

    for i in range(TOPE_BUSQUEDA + 5):
        db.session.add(ResidentBelonging(resident_id=antonia.id,
                                         description=f'Panuelo {i}'))
    db.session.commit()

    datos = client.get('/api/worker/belongings/search',
                       headers=worker_headers).get_json()

    assert datos['total'] == TOPE_BUSQUEDA + 5
    assert datos['mostrados'] == TOPE_BUSQUEDA
    assert len(datos['belongings']) == TOPE_BUSQUEDA
