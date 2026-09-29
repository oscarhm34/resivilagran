"""
test_belongings_reasignar.py — Corregir el inventario desde el panel.

Los dos errores que esto arregla son reales y frecuentes: apuntar un objeto al
residente equivocado, y meter en la misma pertenencia fotos que eran de cosas
distintas. Como las dos operaciones mueven filas entre padres, lo que se
protege aqui es que no se pierda ninguna foto por el camino, que el tope de
seis por objeto siga valiendo y que un objeto no se quede mudo (sin fotos y sin
descripcion) sin que nadie lo borre.

Tambien se cubre el orden, porque de el depende cual es la portada.

Las fotos se escriben en un directorio temporal: la suite nunca toca uploads/.
"""

import io

import pytest
from PIL import Image

from app.models import AuditLog, BelongingPhoto, Resident, ResidentBelonging


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def uploads(app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'UPLOAD_FOLDER', str(tmp_path))
    return tmp_path


@pytest.fixture
def antonia(db):
    r = Resident(name='Antonia Vidal', nfc_code='RES-MOV-1', active=True,
                 room_number='101')
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def benito(db):
    r = Resident(name='Benito Roca', nfc_code='RES-MOV-2', active=True,
                 room_number='102')
    db.session.add(r)
    db.session.commit()
    return r


def _jpeg(color=(30, 70, 200), size=(600, 480)) -> io.BytesIO:
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'JPEG')
    buf.seek(0)
    return buf


def _crear_objeto(db, residente, uploads, descripcion='Jersey azul', fotos=2,
                  categoria='ropa'):
    """Una pertenencia con `fotos` imagenes de verdad en el temporal."""
    from app.blueprints.residents import _save_belonging_photo

    item = ResidentBelonging(resident_id=residente.id, description=descripcion,
                             category=categoria)
    db.session.add(item)
    db.session.flush()
    for i in range(fotos):
        db.session.add(BelongingPhoto(
            belonging_id=item.id,
            photo_path=_save_belonging_photo(_jpeg(), residente.id),
            sort_order=i + 1))
    db.session.commit()
    return item


# ── Autorizacion ──────────────────────────────────────────────────────────────

RUTAS_POST = [
    '/admin/pertenencias/1/mover',
    '/admin/pertenencias/fotos/mover',
    '/admin/pertenencias/1/fotos/orden',
    '/admin/pertenencias/orden',
]
RUTAS_GET = [
    '/admin/pertenencias/1/fotos/json',
    '/admin/pertenencias/residente/1/json',
]


@pytest.mark.parametrize('ruta', RUTAS_POST)
def test_escrituras_sin_sesion_redirigen_al_login(client, db, ruta):
    res = client.post(ruta, json={})
    assert res.status_code in (301, 302)
    assert '/admin/login' in res.headers['Location']


@pytest.mark.parametrize('ruta', RUTAS_GET)
def test_lecturas_sin_sesion_redirigen_al_login(client, db, ruta):
    res = client.get(ruta)
    assert res.status_code in (301, 302)
    assert '/admin/login' in res.headers['Location']


@pytest.mark.parametrize('ruta', RUTAS_POST + RUTAS_GET)
def test_trabajadora_sin_admin_no_puede(client, db, cleaner_user, ruta):
    client.post('/admin/login',
                data={'username': 'limpiadora1', 'password': 'limpia123'},
                follow_redirects=True)
    res = client.post(ruta, json={}) if ruta in RUTAS_POST else client.get(ruta)
    assert res.status_code in (302, 403)


# ── Mover una pertenencia entera ──────────────────────────────────────────────

def test_mover_objeto_cambia_de_residente_y_conserva_las_fotos(
        auth_client, db, antonia, benito, uploads):
    item = _crear_objeto(db, antonia, uploads)
    rutas = sorted(f.photo_path for f in item.photos)

    res = auth_client.post(f'/admin/pertenencias/{item.id}/mover',
                           json={'resident_id': benito.id})

    assert res.status_code == 200
    assert res.get_json()['resident_name'] == 'Benito Roca'
    db.session.expire_all()
    movido = db.session.get(ResidentBelonging, item.id)
    assert movido.resident_id == benito.id
    assert len(movido.photos) == 2
    # Los ficheros no se tocan: quien manda es la clave ajena, no la carpeta.
    assert sorted(f.photo_path for f in movido.photos) == rutas


def test_mover_objeto_lo_pone_arriba_en_el_inventario_de_destino(
        auth_client, db, antonia, benito, uploads):
    item = _crear_objeto(db, antonia, uploads)
    item.sort_order = 7
    db.session.commit()

    auth_client.post(f'/admin/pertenencias/{item.id}/mover',
                     json={'resident_id': benito.id})

    db.session.expire_all()
    assert db.session.get(ResidentBelonging, item.id).sort_order == 0


def test_mover_objeto_deja_rastro_en_la_auditoria(
        auth_client, db, antonia, benito, uploads):
    item = _crear_objeto(db, antonia, uploads)

    auth_client.post(f'/admin/pertenencias/{item.id}/mover',
                     json={'resident_id': benito.id})

    registro = AuditLog.query.filter_by(table_name='resident_belonging',
                                        record_id=item.id, action='update').first()
    assert registro is not None
    assert str(antonia.id) in registro.details
    assert str(benito.id) in registro.details


def test_mover_objeto_a_residente_de_baja_no_hace_nada(
        auth_client, db, antonia, benito, uploads):
    item = _crear_objeto(db, antonia, uploads)
    benito.active = False
    db.session.commit()

    res = auth_client.post(f'/admin/pertenencias/{item.id}/mover',
                           json={'resident_id': benito.id})

    assert res.status_code == 404
    db.session.expire_all()
    assert db.session.get(ResidentBelonging, item.id).resident_id == antonia.id


def test_mover_objeto_al_mismo_residente_se_rechaza(
        auth_client, db, antonia, uploads):
    item = _crear_objeto(db, antonia, uploads)

    res = auth_client.post(f'/admin/pertenencias/{item.id}/mover',
                           json={'resident_id': antonia.id})

    assert res.status_code == 400
    assert 'ya es de ese residente' in res.get_json()['error']


def test_mover_objeto_sin_residente_se_rechaza(auth_client, db, antonia, uploads):
    item = _crear_objeto(db, antonia, uploads)

    res = auth_client.post(f'/admin/pertenencias/{item.id}/mover', json={})

    assert res.status_code == 400


def test_mover_un_objeto_que_no_existe_da_404(auth_client, db, antonia):
    assert auth_client.post('/admin/pertenencias/9999/mover',
                            json={'resident_id': antonia.id}).status_code == 404


# ── Mover fotos a otra pertenencia ────────────────────────────────────────────

def test_mover_una_foto_a_otro_objeto(auth_client, db, antonia, uploads):
    origen = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=2)
    destino = _crear_objeto(db, antonia, uploads, 'Bufanda roja', fotos=1)
    foto_id = origen.photos[1].id

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': [foto_id], 'destino': 'existente',
                                 'item_id': destino.id})

    assert res.status_code == 200
    assert res.get_json()['movidas'] == 1
    db.session.expire_all()
    assert db.session.get(BelongingPhoto, foto_id).belonging_id == destino.id
    assert len(db.session.get(ResidentBelonging, origen.id).photos) == 1
    assert len(db.session.get(ResidentBelonging, destino.id).photos) == 2


def test_la_foto_movida_se_coloca_al_final_del_destino(
        auth_client, db, antonia, uploads):
    origen = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=2)
    destino = _crear_objeto(db, antonia, uploads, 'Bufanda roja', fotos=2)
    foto_id = origen.photos[0].id

    auth_client.post('/admin/pertenencias/fotos/mover',
                     json={'photo_ids': [foto_id], 'destino': 'existente',
                           'item_id': destino.id})

    db.session.expire_all()
    fotos = db.session.get(ResidentBelonging, destino.id).photos
    assert [f.id for f in fotos][-1] == foto_id
    assert fotos[-1].sort_order == 3


def test_mover_fotos_a_un_objeto_de_otro_residente(
        auth_client, db, antonia, benito, uploads):
    origen = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=2)
    destino = _crear_objeto(db, benito, uploads, 'Jersey de Benito', fotos=1)
    foto_id = origen.photos[0].id

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': [foto_id], 'destino': 'existente',
                                 'item_id': destino.id})

    assert res.status_code == 200
    db.session.expire_all()
    assert db.session.get(BelongingPhoto, foto_id).belonging.resident_id == benito.id


def test_no_se_pasa_del_tope_de_seis_fotos(auth_client, db, antonia, uploads):
    origen = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=2)
    destino = _crear_objeto(db, antonia, uploads, 'Maleta', fotos=5)
    ids = [f.id for f in origen.photos]

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': ids, 'destino': 'existente',
                                 'item_id': destino.id})

    assert res.status_code == 400
    assert 'Solo caben 1' in res.get_json()['error']
    db.session.expire_all()
    assert len(db.session.get(ResidentBelonging, origen.id).photos) == 2


def test_el_objeto_vacio_sin_descripcion_se_borra(auth_client, db, antonia, uploads):
    origen = _crear_objeto(db, antonia, uploads, descripcion=None, fotos=2)
    destino = _crear_objeto(db, antonia, uploads, 'Bufanda roja', fotos=1)
    ids = [f.id for f in origen.photos]

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': ids, 'destino': 'existente',
                                 'item_id': destino.id})

    assert res.status_code == 200
    assert 'se ha eliminado' in res.get_json()['mensaje']
    db.session.expire_all()
    assert db.session.get(ResidentBelonging, origen.id) is None
    assert len(db.session.get(ResidentBelonging, destino.id).photos) == 3


def test_el_objeto_vacio_con_descripcion_sobrevive(auth_client, db, antonia, uploads):
    origen = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=1)
    destino = _crear_objeto(db, antonia, uploads, 'Bufanda roja', fotos=1)

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': [origen.photos[0].id],
                                 'destino': 'existente', 'item_id': destino.id})

    assert res.status_code == 200
    db.session.expire_all()
    superviviente = db.session.get(ResidentBelonging, origen.id)
    assert superviviente is not None
    assert superviviente.photos == []


def test_mover_fotos_al_objeto_donde_ya_estan_se_rechaza(
        auth_client, db, antonia, uploads):
    item = _crear_objeto(db, antonia, uploads, fotos=2)

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': [item.photos[0].id],
                                 'destino': 'existente', 'item_id': item.id})

    assert res.status_code == 400
    assert 'ya estan' in res.get_json()['error']


def test_mover_una_foto_que_no_existe_da_404(auth_client, db, antonia, uploads):
    destino = _crear_objeto(db, antonia, uploads)
    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': [9999], 'destino': 'existente',
                                 'item_id': destino.id})
    assert res.status_code == 404


def test_mover_sin_fotos_se_rechaza(auth_client, db, antonia, uploads):
    destino = _crear_objeto(db, antonia, uploads)
    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': [], 'destino': 'existente',
                                 'item_id': destino.id})
    assert res.status_code == 400


# ── Separar fotos en una pertenencia nueva ────────────────────────────────────

def test_separar_fotos_crea_una_pertenencia_nueva(
        auth_client, db, antonia, uploads, admin_user):
    origen = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=3)
    ids = [origen.photos[1].id, origen.photos[2].id]

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': ids, 'destino': 'nueva',
                                 'resident_id': antonia.id, 'category': 'complementos',
                                 'description': 'Gafas de pasta'})

    assert res.status_code == 200
    db.session.expire_all()
    nueva = ResidentBelonging.query.filter_by(description='Gafas de pasta').first()
    assert nueva is not None
    assert nueva.resident_id == antonia.id
    assert nueva.category == 'complementos'
    assert nueva.created_by == admin_user.id
    assert sorted(f.id for f in nueva.photos) == sorted(ids)
    assert [f.sort_order for f in nueva.photos] == [1, 2]
    assert len(db.session.get(ResidentBelonging, origen.id).photos) == 1
    # La auditoria de cada foto tiene que saber a donde fue, no a "ninguna".
    rastro = AuditLog.query.filter_by(table_name='belonging_photo',
                                      record_id=ids[0]).first()
    assert f'"belonging_id": {nueva.id}' in rastro.details


def test_separar_fotos_a_otro_residente(auth_client, db, antonia, benito, uploads):
    origen = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=2)

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': [origen.photos[0].id], 'destino': 'nueva',
                                 'resident_id': benito.id, 'description': 'Boina'})

    assert res.status_code == 200
    nueva = ResidentBelonging.query.filter_by(description='Boina').first()
    assert nueva.resident_id == benito.id


def test_separar_a_un_residente_de_baja_se_rechaza(
        auth_client, db, antonia, benito, uploads):
    origen = _crear_objeto(db, antonia, uploads, fotos=2)
    benito.active = False
    db.session.commit()

    res = auth_client.post('/admin/pertenencias/fotos/mover',
                           json={'photo_ids': [origen.photos[0].id], 'destino': 'nueva',
                                 'resident_id': benito.id, 'description': 'Boina'})

    assert res.status_code == 404
    assert ResidentBelonging.query.filter_by(description='Boina').first() is None


def test_separar_con_una_categoria_inventada_la_deja_sin_categoria(
        auth_client, db, antonia, uploads):
    origen = _crear_objeto(db, antonia, uploads, fotos=2)

    auth_client.post('/admin/pertenencias/fotos/mover',
                     json={'photo_ids': [origen.photos[0].id], 'destino': 'nueva',
                           'resident_id': antonia.id, 'category': 'joyas',
                           'description': 'Anillo'})

    assert ResidentBelonging.query.filter_by(description='Anillo').first().category is None


# ── Orden de las fotos ────────────────────────────────────────────────────────

def test_reordenar_fotos_cambia_la_portada(auth_client, db, antonia, uploads):
    item = _crear_objeto(db, antonia, uploads, fotos=3)
    ids = [f.id for f in item.photos]
    nuevo = [ids[2], ids[0], ids[1]]

    res = auth_client.post(f'/admin/pertenencias/{item.id}/fotos/orden',
                           json={'photo_ids': nuevo})

    assert res.status_code == 200
    db.session.expire_all()
    recargado = db.session.get(ResidentBelonging, item.id)
    assert [f.id for f in recargado.photos] == nuevo
    assert [f.sort_order for f in recargado.photos] == [1, 2, 3]
    assert recargado.cover.id == nuevo[0]


def test_reordenar_con_una_foto_ajena_no_toca_nada(
        auth_client, db, antonia, uploads):
    item = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=2)
    otro = _crear_objeto(db, antonia, uploads, 'Bufanda roja', fotos=1)
    original = [f.id for f in item.photos]

    res = auth_client.post(f'/admin/pertenencias/{item.id}/fotos/orden',
                           json={'photo_ids': [original[0], otro.photos[0].id]})

    assert res.status_code == 400
    db.session.expire_all()
    assert [f.id for f in db.session.get(ResidentBelonging, item.id).photos] == original


def test_reordenar_con_una_foto_de_menos_se_rechaza(auth_client, db, antonia, uploads):
    item = _crear_objeto(db, antonia, uploads, fotos=3)

    res = auth_client.post(f'/admin/pertenencias/{item.id}/fotos/orden',
                           json={'photo_ids': [item.photos[0].id]})

    assert res.status_code == 400


def test_reordenar_con_ids_repetidos_se_rechaza(auth_client, db, antonia, uploads):
    item = _crear_objeto(db, antonia, uploads, fotos=2)
    primero = item.photos[0].id

    res = auth_client.post(f'/admin/pertenencias/{item.id}/fotos/orden',
                           json={'photo_ids': [primero, primero]})

    assert res.status_code == 400


# ── Orden de los objetos del residente ────────────────────────────────────────

def test_reordenar_el_inventario_de_un_residente(auth_client, db, antonia, uploads):
    uno = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=1)
    dos = _crear_objeto(db, antonia, uploads, 'Bufanda roja', fotos=1)
    tres = _crear_objeto(db, antonia, uploads, 'Boina', fotos=1)

    res = auth_client.post('/admin/pertenencias/orden',
                           json={'resident_id': antonia.id,
                                 'item_ids': [dos.id, tres.id, uno.id]})

    assert res.status_code == 200
    db.session.expire_all()
    orden = ResidentBelonging.query.filter_by(resident_id=antonia.id).order_by(
        ResidentBelonging.sort_order).all()
    assert [i.id for i in orden] == [dos.id, tres.id, uno.id]


def test_reordenar_con_un_objeto_de_otro_residente_se_rechaza(
        auth_client, db, antonia, benito, uploads):
    mio = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=1)
    ajeno = _crear_objeto(db, benito, uploads, 'Jersey de Benito', fotos=1)

    res = auth_client.post('/admin/pertenencias/orden',
                           json={'resident_id': antonia.id,
                                 'item_ids': [mio.id, ajeno.id]})

    assert res.status_code == 400
    db.session.expire_all()
    assert db.session.get(ResidentBelonging, mio.id).sort_order == 0


def test_el_listado_respeta_el_orden_manual(auth_client, db, antonia, uploads):
    # Ordenar reescribe todos los objetos del residente con 1..N, asi que el
    # listado tiene que hacer caso a esos numeros y no a la fecha.
    primero = _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=1)
    segundo = _crear_objeto(db, antonia, uploads, 'Bufanda roja', fotos=1)
    segundo.sort_order = 1
    primero.sort_order = 2
    db.session.commit()

    texto = auth_client.get(
        f'/admin/pertenencias?resident_id={antonia.id}').get_data(as_text=True)
    assert texto.index('Bufanda roja') < texto.index('Jersey azul')


def test_un_objeto_recien_movido_sale_arriba(auth_client, db, antonia, benito, uploads):
    """El que llega de otro residente va con sort_order 0: lo primero que se mira."""
    _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=1).sort_order = 1
    llegado = _crear_objeto(db, benito, uploads, 'Bufanda roja', fotos=1)
    db.session.commit()

    auth_client.post(f'/admin/pertenencias/{llegado.id}/mover',
                     json={'resident_id': antonia.id})

    texto = auth_client.get(
        f'/admin/pertenencias?resident_id={antonia.id}').get_data(as_text=True)
    assert texto.index('Bufanda roja') < texto.index('Jersey azul')


# ── Lecturas que alimentan los modales ────────────────────────────────────────

def test_las_fotos_de_un_objeto_salen_en_orden(auth_client, db, antonia, uploads):
    item = _crear_objeto(db, antonia, uploads, fotos=3)
    esperado = [f.id for f in item.photos]

    datos = auth_client.get(f'/admin/pertenencias/{item.id}/fotos/json').get_json()

    assert [f['id'] for f in datos['photos']] == esperado
    assert datos['resident_name'] == 'Antonia Vidal'
    assert datos['max_photos'] == 6
    assert datos['photos'][0]['url'].startswith('/uploads/')


def test_los_objetos_de_un_residente_dicen_cuanto_hueco_les_queda(
        auth_client, db, antonia, uploads):
    _crear_objeto(db, antonia, uploads, 'Jersey azul', fotos=2)

    datos = auth_client.get(
        f'/admin/pertenencias/residente/{antonia.id}/json').get_json()

    assert len(datos['belongings']) == 1
    assert datos['belongings'][0]['photo_count'] == 2
    assert datos['belongings'][0]['hueco'] == 4
    assert datos['belongings'][0]['category_label'] == 'Ropa'


def test_los_objetos_de_un_residente_de_baja_no_se_sirven(
        auth_client, db, antonia, uploads):
    _crear_objeto(db, antonia, uploads)
    antonia.active = False
    db.session.commit()

    res = auth_client.get(f'/admin/pertenencias/residente/{antonia.id}/json')
    assert res.status_code == 404


# ── Interaccion con lo que ya habia ───────────────────────────────────────────

def test_las_fotos_nuevas_se_colocan_al_final(auth_client, db, antonia, uploads):
    """Anadir no puede adelantar a la portada que alguien haya elegido."""
    item = _crear_objeto(db, antonia, uploads, fotos=2)
    portada = item.photos[0].id

    res = auth_client.post(f'/admin/pertenencias/{item.id}/fotos',
                           data={'photos': (_jpeg(), 'nueva.jpg')},
                           content_type='multipart/form-data')

    assert res.status_code == 302
    db.session.expire_all()
    recargado = db.session.get(ResidentBelonging, item.id)
    assert len(recargado.photos) == 3
    assert recargado.cover.id == portada
    assert recargado.photos[-1].sort_order == 3
