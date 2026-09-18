"""
test_belongings.py — Inventario de pertenencias de los residentes.

Lo que se protege aqui: que las fotos de objetos personales solo salgan con
token, que quien registra sea siempre la trabajadora del JWT y no la que diga
el cuerpo de la peticion, y que un elemento no pueda quedarse mudo (ni foto ni
descripcion), porque entonces no sirve de nada.

Las fotos se escriben en un directorio temporal: la suite nunca toca uploads/.
"""

import io
import os

import pytest
from flask_jwt_extended import create_access_token
from PIL import Image

from app.models import BelongingPhoto, Cleaner, Resident, ResidentBelonging


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def admin_headers(db, admin_user, app):
    """Cabecera JWT de una administradora: la unica que puede eliminar."""
    with app.app_context():
        token = create_access_token(identity=admin_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def otra_trabajadora(db):
    """Segunda trabajadora, para comprobar de quien queda constancia."""
    u = Cleaner(username='limpiadora2', name='Otra Trabajadora', is_admin=False)
    u.set_password('limpia456')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def residente(db):
    r = Resident(name='Antonia Vidal', nfc_code='RES-INV-1', active=True,
                 room_number='101')
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def otro_residente(db):
    r = Resident(name='Josep Roca', nfc_code='RES-INV-2', active=True)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def uploads(app, tmp_path, monkeypatch):
    """Redirige UPLOAD_FOLDER a un temporal para esta prueba."""
    monkeypatch.setitem(app.config, 'UPLOAD_FOLDER', str(tmp_path))
    return tmp_path


def _jpeg(color=(200, 30, 30), size=(1400, 900)) -> io.BytesIO:
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'JPEG')
    buf.seek(0)
    return buf


# ── Sin token no se entra ─────────────────────────────────────────────────────

def test_listar_sin_token_devuelve_401(client, db, residente):
    assert client.get(f'/api/worker/resident/{residente.id}/belongings').status_code == 401


def test_crear_sin_token_devuelve_401(client, db, residente):
    res = client.post(f'/api/worker/resident/{residente.id}/belongings',
                      data={'description': 'jersey azul'})
    assert res.status_code == 401


def test_editar_sin_token_devuelve_401(client, db):
    assert client.post('/api/worker/belongings/1/update', json={}).status_code == 401


def test_eliminar_sin_token_devuelve_401(client, db):
    assert client.post('/api/worker/belongings/1/delete').status_code == 401


# ── Crear ─────────────────────────────────────────────────────────────────────

def test_crear_solo_con_texto(client, db, residente, cleaner_user, worker_headers):
    res = client.post(f'/api/worker/resident/{residente.id}/belongings',
                      headers=worker_headers,
                      data={'description': 'Peine de madera', 'category': 'higiene'})

    assert res.status_code == 201
    item = ResidentBelonging.query.one()
    assert item.description == 'Peine de madera'
    assert item.category == 'higiene'
    assert item.photos == []
    assert item.created_by == cleaner_user.id


def test_el_registrador_sale_del_token_no_del_cuerpo(
        client, db, residente, cleaner_user, otra_trabajadora, worker_headers):
    """Mandar un created_by ajeno no puede atribuirle el registro a otra."""
    res = client.post(f'/api/worker/resident/{residente.id}/belongings',
                      headers=worker_headers,
                      data={'description': 'Cinturon marron',
                            'created_by': otra_trabajadora.id,
                            'worker_id': otra_trabajadora.id})

    assert res.status_code == 201
    assert ResidentBelonging.query.one().created_by == cleaner_user.id


def test_crear_con_foto_la_guarda_reprocesada(
        client, db, residente, worker_headers, uploads):
    res = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photo': (_jpeg(), 'objeto.jpg'), 'description': 'Jersey azul'},
        content_type='multipart/form-data')

    assert res.status_code == 201
    item = ResidentBelonging.query.one()
    assert len(item.photos) == 1
    ruta = item.photos[0].photo_path
    assert ruta.startswith(f'belongings/res_{residente.id}/')
    destino = os.path.join(str(uploads), ruta)
    assert os.path.exists(destino)
    with Image.open(destino) as img:
        assert img.format == 'JPEG'          # ha pasado por el reprocesado
        assert max(img.size) <= 1000         # y por el redimensionado
    assert res.get_json()['belonging']['photo_url'] == f'/api/uploads/{ruta}'


def test_sin_foto_y_sin_texto_se_rechaza(client, db, residente, worker_headers):
    res = client.post(f'/api/worker/resident/{residente.id}/belongings',
                      headers=worker_headers, data={'description': '   '})

    assert res.status_code == 400
    assert ResidentBelonging.query.count() == 0


def test_un_ejecutable_disfrazado_se_rechaza(
        client, db, residente, worker_headers, uploads):
    res = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photo': (io.BytesIO(b'MZ\x90\x00'), 'virus.exe')},
        content_type='multipart/form-data')

    assert res.status_code == 400
    assert ResidentBelonging.query.count() == 0


def test_categoria_desconocida_se_ignora(client, db, residente, worker_headers):
    res = client.post(f'/api/worker/resident/{residente.id}/belongings',
                      headers=worker_headers,
                      data={'description': 'Maquinilla', 'category': 'inventada'})

    assert res.status_code == 201
    assert ResidentBelonging.query.one().category is None


def test_residente_de_baja_no_admite_pertenencias(
        client, db, residente, worker_headers):
    residente.active = False
    db.session.commit()

    res = client.post(f'/api/worker/resident/{residente.id}/belongings',
                      headers=worker_headers, data={'description': 'Pantalon'})

    assert res.status_code == 404


def test_residente_inexistente_devuelve_404(client, db, worker_headers):
    assert client.get('/api/worker/resident/9999/belongings',
                      headers=worker_headers).status_code == 404


# ── Varias fotos por objeto ───────────────────────────────────────────────────

def test_crear_con_varias_fotos_de_una_vez(
        client, db, residente, worker_headers, uploads):
    """El conjunto, la etiqueta de la talla y el remiendo del puno, a la vez."""
    res = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photos': [(_jpeg((200, 30, 30)), 'a.jpg'),
                         (_jpeg((30, 30, 200)), 'b.jpg'),
                         (_jpeg((30, 200, 30)), 'c.jpg')],
              'description': 'Jersey de lana'},
        content_type='multipart/form-data')

    assert res.status_code == 201
    datos = res.get_json()['belonging']
    assert datos['photo_count'] == 3
    assert len(datos['photos']) == 3
    assert datos['photo_url'] == datos['photos'][0]['url']      # la primera, de portada
    item = ResidentBelonging.query.one()
    assert len(item.photos) == 3
    rutas = {p.photo_path for p in item.photos}
    assert len(rutas) == 3, 'cada foto necesita su propio fichero'
    for ruta in rutas:
        assert os.path.exists(os.path.join(str(uploads), ruta))


def test_anadir_una_foto_a_un_objeto_ya_registrado(
        client, db, residente, cleaner_user, worker_headers, uploads):
    item = ResidentBelonging(resident_id=residente.id, description='Jersey',
                             created_by=cleaner_user.id)
    db.session.add(item)
    db.session.commit()

    res = client.post(f'/api/worker/belongings/{item.id}/photos',
                      headers=worker_headers,
                      data={'photos': (_jpeg(), 'etiqueta.jpg')},
                      content_type='multipart/form-data')

    assert res.status_code == 201
    assert res.get_json()['belonging']['photo_count'] == 1
    assert len(db.session.get(ResidentBelonging, item.id).photos) == 1
    assert item.updated_at is not None


def test_anadir_fotos_sin_ninguna_se_rechaza(
        client, db, residente, cleaner_user, worker_headers):
    item = ResidentBelonging(resident_id=residente.id, description='Jersey',
                             created_by=cleaner_user.id)
    db.session.add(item)
    db.session.commit()

    res = client.post(f'/api/worker/belongings/{item.id}/photos',
                      headers=worker_headers, data={})

    assert res.status_code == 400


def test_no_se_pasa_del_tope_de_fotos(
        client, db, residente, worker_headers, uploads):
    from app.blueprints.residents import MAX_FOTOS_POR_OBJETO

    res = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photos': [(_jpeg(), f'{i}.jpg') for i in range(MAX_FOTOS_POR_OBJETO + 1)]},
        content_type='multipart/form-data')

    assert res.status_code == 400
    assert ResidentBelonging.query.count() == 0
    escritos = [f for _, _, fs in os.walk(str(uploads)) for f in fs]
    assert escritos == [], 'un lote rechazado no puede dejar ficheros sueltos'


def test_una_foto_invalida_no_deja_a_medias_el_lote(
        client, db, residente, worker_headers, uploads):
    """Si la segunda no vale, la primera tampoco se queda en el disco."""
    res = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photos': [(_jpeg(), 'buena.jpg'),
                         (io.BytesIO(b'MZ\x90\x00'), 'virus.exe')]},
        content_type='multipart/form-data')

    assert res.status_code == 400
    assert ResidentBelonging.query.count() == 0
    escritos = [f for _, _, fs in os.walk(str(uploads)) for f in fs]
    assert escritos == []


def test_administracion_elimina_una_foto_suelta(
        client, db, residente, worker_headers, admin_headers, uploads):
    creada = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photos': [(_jpeg(), 'a.jpg'), (_jpeg(), 'b.jpg')],
              'description': 'Jersey'},
        content_type='multipart/form-data').get_json()['belonging']
    foto_id = creada['photos'][0]['id']
    ruta = os.path.join(str(uploads),
                        db.session.get(BelongingPhoto, foto_id).photo_path)

    res = client.post(f'/api/worker/belongings/photos/{foto_id}/delete',
                      headers=admin_headers)

    assert res.status_code == 200
    assert res.get_json()['belonging']['photo_count'] == 1
    assert db.session.get(BelongingPhoto, foto_id) is None
    assert not os.path.exists(ruta)


def test_una_trabajadora_no_puede_eliminar_una_foto(
        client, db, residente, worker_headers, uploads):
    creada = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photos': [(_jpeg(), 'a.jpg'), (_jpeg(), 'b.jpg')]},
        content_type='multipart/form-data').get_json()['belonging']

    res = client.post(f"/api/worker/belongings/photos/{creada['photos'][0]['id']}/delete",
                      headers=worker_headers)

    assert res.status_code == 403
    assert db.session.get(BelongingPhoto, creada['photos'][0]['id']) is not None


def test_no_se_borra_la_ultima_foto_de_un_objeto_sin_descripcion(
        client, db, residente, worker_headers, admin_headers, uploads):
    """Quedaria una fila que no dice nada: o describe el objeto, o se borra entero."""
    creada = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photos': (_jpeg(), 'a.jpg')},
        content_type='multipart/form-data').get_json()['belonging']

    res = client.post(f"/api/worker/belongings/photos/{creada['photos'][0]['id']}/delete",
                      headers=admin_headers)

    assert res.status_code == 400
    assert db.session.get(ResidentBelonging, creada['id']).photos


def test_eliminar_el_objeto_se_lleva_todas_sus_fotos(
        client, db, residente, worker_headers, admin_headers, uploads):
    creada = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photos': [(_jpeg(), 'a.jpg'), (_jpeg(), 'b.jpg')]},
        content_type='multipart/form-data').get_json()['belonging']
    rutas = [os.path.join(str(uploads), p.photo_path)
             for p in db.session.get(ResidentBelonging, creada['id']).photos]

    res = client.post(f"/api/worker/belongings/{creada['id']}/delete",
                      headers=admin_headers)

    assert res.status_code == 200
    assert BelongingPhoto.query.count() == 0
    assert not any(os.path.exists(r) for r in rutas)


# ── Listar ────────────────────────────────────────────────────────────────────

def test_listar_solo_devuelve_las_del_residente_pedido(
        client, db, residente, otro_residente, cleaner_user, worker_headers):
    db.session.add_all([
        ResidentBelonging(resident_id=residente.id, description='Bufanda roja',
                          created_by=cleaner_user.id),
        ResidentBelonging(resident_id=otro_residente.id, description='Gorra',
                          created_by=cleaner_user.id),
    ])
    db.session.commit()

    datos = client.get(f'/api/worker/resident/{residente.id}/belongings',
                       headers=worker_headers).get_json()

    descripciones = [b['description'] for b in datos['belongings']]
    assert descripciones == ['Bufanda roja']
    assert datos['belongings'][0]['created_by_name'] == cleaner_user.name


# ── Editar ────────────────────────────────────────────────────────────────────

def test_editar_la_descripcion(client, db, residente, cleaner_user, worker_headers):
    item = ResidentBelonging(resident_id=residente.id, description='Jersey',
                             created_by=cleaner_user.id)
    db.session.add(item)
    db.session.commit()

    res = client.post(f'/api/worker/belongings/{item.id}/update',
                      headers=worker_headers,
                      json={'description': 'Jersey azul de lana', 'category': 'ropa'})

    assert res.status_code == 200
    assert item.description == 'Jersey azul de lana'
    assert item.category == 'ropa'
    assert item.updated_at is not None


def test_no_se_puede_dejar_un_elemento_mudo(
        client, db, residente, cleaner_user, worker_headers):
    """Sin foto, borrar la descripcion dejaria una fila que no dice nada."""
    item = ResidentBelonging(resident_id=residente.id, description='Peine',
                             created_by=cleaner_user.id)
    db.session.add(item)
    db.session.commit()

    res = client.post(f'/api/worker/belongings/{item.id}/update',
                      headers=worker_headers, json={'description': ''})

    assert res.status_code == 400
    assert db.session.get(ResidentBelonging, item.id).description == 'Peine'


# ── Eliminar ──────────────────────────────────────────────────────────────────

def test_eliminar_borra_la_fila_y_la_foto(
        client, db, residente, worker_headers, admin_headers, uploads):
    creada = client.post(
        f'/api/worker/resident/{residente.id}/belongings',
        headers=worker_headers,
        data={'photo': (_jpeg(), 'objeto.jpg')},
        content_type='multipart/form-data').get_json()
    item_id = creada['belonging']['id']
    ruta = os.path.join(str(uploads),
                        db.session.get(ResidentBelonging, item_id).photos[0].photo_path)
    assert os.path.exists(ruta)

    res = client.post(f'/api/worker/belongings/{item_id}/delete', headers=admin_headers)

    assert res.status_code == 200
    assert db.session.get(ResidentBelonging, item_id) is None
    assert not os.path.exists(ruta)


def test_una_trabajadora_no_puede_eliminar(
        client, db, residente, cleaner_user, worker_headers):
    """Borrar la foto no se deshace: queda para administracion."""
    item = ResidentBelonging(resident_id=residente.id, description='Peine',
                             created_by=cleaner_user.id)
    db.session.add(item)
    db.session.commit()

    res = client.post(f'/api/worker/belongings/{item.id}/delete', headers=worker_headers)

    assert res.status_code == 403
    assert db.session.get(ResidentBelonging, item.id) is not None


def test_eliminar_una_pertenencia_inexistente_devuelve_404(client, db, admin_headers):
    assert client.post('/api/worker/belongings/9999/delete',
                       headers=admin_headers).status_code == 404


# ── La ficha del panel de administracion ──────────────────────────────────────

def test_la_ficha_del_residente_muestra_su_inventario(
        auth_client, db, residente, cleaner_user):
    item = ResidentBelonging(
        resident_id=residente.id, description='Cinturon marron de piel',
        category='complementos', created_by=cleaner_user.id)
    item.photos.append(BelongingPhoto(photo_path=f'belongings/res_{residente.id}/x.jpg'))
    db.session.add(item)
    db.session.commit()

    html = auth_client.get(f'/admin/resident/{residente.id}').get_data(as_text=True)

    assert 'tab-inventario' in html
    assert 'Cinturon marron de piel' in html
    assert 'Complementos' in html
    assert cleaner_user.name in html


def test_la_ficha_lo_dice_cuando_no_hay_nada_registrado(auth_client, db, residente):
    html = auth_client.get(f'/admin/resident/{residente.id}').get_data(as_text=True)

    assert 'tab-inventario' in html
    assert 'no tiene pertenencias registradas' in html


def test_la_ficha_del_residente_necesita_admin(client, db, residente):
    res = client.get(f'/admin/resident/{residente.id}')

    assert res.status_code in (302, 401, 403)
