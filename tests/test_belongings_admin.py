"""
test_belongings_admin.py — Gestion del inventario desde el panel de admin.

Lo que se protege aqui: que las rutas nuevas del panel no se puedan usar sin ser
administrador, que borrar quite de verdad el fichero del disco (no solo la fila)
y que las reglas de negocio que ya valian en la webapp —el objeto no puede
quedarse mudo, el fichero tiene que ser una imagen— sigan valiendo por esta otra
puerta.

Las fotos se escriben en un directorio temporal: la suite nunca toca uploads/.
"""

import io
import os

import pytest
from PIL import Image

from app.models import BelongingPhoto, Resident, ResidentBelonging


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def residente(db):
    r = Resident(name='Antonia Vidal', nfc_code='RES-ADM-1', active=True,
                 room_number='101')
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def uploads(app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'UPLOAD_FOLDER', str(tmp_path))
    return tmp_path


def _jpeg(color=(30, 70, 200), size=(900, 700)) -> io.BytesIO:
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'JPEG')
    buf.seek(0)
    return buf


@pytest.fixture
def objeto_con_fotos(db, residente, uploads, app):
    """Una pertenencia con dos fotos de verdad escritas en el temporal."""
    from app.blueprints.residents import _save_belonging_photo

    item = ResidentBelonging(resident_id=residente.id, description='Jersey azul',
                             category='ropa')
    db.session.add(item)
    db.session.flush()
    for _ in range(2):
        ruta = _save_belonging_photo(_jpeg(), residente.id)
        db.session.add(BelongingPhoto(belonging_id=item.id, photo_path=ruta))
    db.session.commit()
    return item


# ── Autorizacion: lo primero, la proteccion de cada ruta es manual ───────────

def test_listado_sin_sesion_redirige_al_login(client, db):
    res = client.get('/admin/pertenencias')
    assert res.status_code in (301, 302)
    assert '/admin/login' in res.headers['Location']


@pytest.mark.parametrize('ruta', [
    '/admin/pertenencias/1/eliminar',
    '/admin/pertenencias/fotos/1/eliminar',
    '/admin/pertenencias/1/editar',
    '/admin/pertenencias/1/fotos',
])
def test_escrituras_sin_sesion_redirigen_al_login(client, db, ruta):
    res = client.post(ruta)
    assert res.status_code in (301, 302)
    assert '/admin/login' in res.headers['Location']


def test_trabajadora_sin_admin_no_entra_al_listado(client, db, cleaner_user):
    client.post('/admin/login',
                data={'username': 'limpiadora1', 'password': 'limpia123'},
                follow_redirects=True)
    res = client.get('/admin/pertenencias')
    assert res.status_code in (302, 403)


# ── Listado y filtros ─────────────────────────────────────────────────────────

def test_el_listado_muestra_las_pertenencias(auth_client, db, objeto_con_fotos):
    res = auth_client.get('/admin/pertenencias')
    assert res.status_code == 200
    assert 'Jersey azul' in res.get_data(as_text=True)


def test_filtro_por_categoria(auth_client, db, residente, objeto_con_fotos):
    db.session.add(ResidentBelonging(resident_id=residente.id,
                                     description='Maquinilla', category='higiene'))
    db.session.commit()

    texto = auth_client.get('/admin/pertenencias?category=higiene').get_data(as_text=True)
    assert 'Maquinilla' in texto
    assert 'Jersey azul' not in texto


def test_filtro_por_texto_de_la_descripcion(auth_client, db, residente):
    db.session.add(ResidentBelonging(resident_id=residente.id, description='Gafas de leer'))
    db.session.add(ResidentBelonging(resident_id=residente.id, description='Bufanda roja'))
    db.session.commit()

    texto = auth_client.get('/admin/pertenencias?q=gafas').get_data(as_text=True)
    assert 'Gafas de leer' in texto
    assert 'Bufanda roja' not in texto


def test_filtro_por_color(auth_client, db, residente, uploads, app):
    """El color va en la foto: basta con que una de ellas sea de ese color."""
    from app.blueprints.residents import _save_belonging_photo

    azul = ResidentBelonging(resident_id=residente.id, description='Jersey de invierno')
    rojo = ResidentBelonging(resident_id=residente.id, description='Bufanda de lana')
    db.session.add_all([azul, rojo])
    db.session.flush()
    db.session.add(BelongingPhoto(
        belonging_id=azul.id, color='azul',
        photo_path=_save_belonging_photo(_jpeg(), residente.id)))
    db.session.add(BelongingPhoto(
        belonging_id=rojo.id, color='rojo',
        photo_path=_save_belonging_photo(_jpeg(), residente.id)))
    db.session.commit()

    texto = auth_client.get('/admin/pertenencias?color=azul').get_data(as_text=True)
    assert 'Jersey de invierno' in texto
    assert 'Bufanda de lana' not in texto


def test_un_residente_de_baja_no_sale_en_el_listado(auth_client, db, residente):
    db.session.add(ResidentBelonging(resident_id=residente.id, description='Reloj viejo'))
    residente.active = False
    db.session.commit()

    assert 'Reloj viejo' not in auth_client.get('/admin/pertenencias').get_data(as_text=True)


# ── Borrar ────────────────────────────────────────────────────────────────────

def test_borrar_una_foto_la_quita_del_disco(auth_client, db, objeto_con_fotos, uploads):
    foto = objeto_con_fotos.photos[0]
    foto_id = foto.id
    ruta = os.path.join(str(uploads), foto.photo_path)
    assert os.path.exists(ruta)

    res = auth_client.post(f'/admin/pertenencias/fotos/{foto_id}/eliminar')

    assert res.status_code == 302
    assert db.session.get(BelongingPhoto, foto_id) is None
    assert not os.path.exists(ruta)
    # El objeto sigue, con la otra foto.
    assert len(db.session.get(ResidentBelonging, objeto_con_fotos.id).photos) == 1


def test_borrar_el_objeto_arrastra_todas_sus_fotos(
        auth_client, db, objeto_con_fotos, uploads):
    item_id = objeto_con_fotos.id
    rutas = [os.path.join(str(uploads), p.photo_path) for p in objeto_con_fotos.photos]
    assert all(os.path.exists(r) for r in rutas)

    res = auth_client.post(f'/admin/pertenencias/{item_id}/eliminar')

    assert res.status_code == 302
    assert db.session.get(ResidentBelonging, item_id) is None
    assert BelongingPhoto.query.filter_by(belonging_id=item_id).count() == 0
    assert not any(os.path.exists(r) for r in rutas)


def test_no_se_borra_la_ultima_foto_si_el_objeto_no_tiene_descripcion(
        auth_client, db, residente, uploads, app):
    """Un objeto sin foto y sin descripcion no dice nada: se rechaza."""
    from app.blueprints.residents import _save_belonging_photo

    item = ResidentBelonging(resident_id=residente.id, description=None)
    db.session.add(item)
    db.session.flush()
    ruta = _save_belonging_photo(_jpeg(), residente.id)
    foto = BelongingPhoto(belonging_id=item.id, photo_path=ruta)
    db.session.add(foto)
    db.session.commit()
    foto_id = foto.id

    auth_client.post(f'/admin/pertenencias/fotos/{foto_id}/eliminar')

    assert db.session.get(BelongingPhoto, foto_id) is not None
    assert os.path.exists(os.path.join(str(uploads), ruta))


def test_borrar_algo_que_no_existe_da_404(auth_client, db):
    assert auth_client.post('/admin/pertenencias/9999/eliminar').status_code == 404
    assert auth_client.post('/admin/pertenencias/fotos/9999/eliminar').status_code == 404


# ── Editar ────────────────────────────────────────────────────────────────────

def test_editar_descripcion_y_categoria(auth_client, db, objeto_con_fotos):
    res = auth_client.post(f'/admin/pertenencias/{objeto_con_fotos.id}/editar',
                           data={'description': 'Jersey azul de cuello alto',
                                 'category': 'complementos'})

    assert res.status_code == 302
    item = db.session.get(ResidentBelonging, objeto_con_fotos.id)
    assert item.description == 'Jersey azul de cuello alto'
    assert item.category == 'complementos'


def test_una_categoria_inventada_se_ignora(auth_client, db, objeto_con_fotos):
    auth_client.post(f'/admin/pertenencias/{objeto_con_fotos.id}/editar',
                     data={'description': 'Jersey azul', 'category': 'pirata'})

    assert db.session.get(ResidentBelonging, objeto_con_fotos.id).category is None


def test_no_se_deja_un_objeto_sin_foto_y_sin_descripcion(auth_client, db, residente):
    item = ResidentBelonging(resident_id=residente.id, description='Solo texto')
    db.session.add(item)
    db.session.commit()

    auth_client.post(f'/admin/pertenencias/{item.id}/editar',
                     data={'description': '   ', 'category': ''})

    assert db.session.get(ResidentBelonging, item.id).description == 'Solo texto'


# ── Anadir fotos desde el ordenador ───────────────────────────────────────────

def test_subir_una_foto_desde_el_panel(auth_client, db, objeto_con_fotos, uploads):
    antes = len(objeto_con_fotos.photos)

    res = auth_client.post(
        f'/admin/pertenencias/{objeto_con_fotos.id}/fotos',
        data={'photos': (_jpeg(), 'nueva.jpg')},
        content_type='multipart/form-data')

    assert res.status_code == 302
    item = db.session.get(ResidentBelonging, objeto_con_fotos.id)
    assert len(item.photos) == antes + 1
    assert os.path.exists(os.path.join(str(uploads), item.photos[-1].photo_path))


def test_la_foto_subida_guarda_su_color(auth_client, db, objeto_con_fotos, uploads):
    auth_client.post(f'/admin/pertenencias/{objeto_con_fotos.id}/fotos',
                     data={'photos': (_jpeg((30, 70, 200)), 'azul.jpg')},
                     content_type='multipart/form-data')

    item = db.session.get(ResidentBelonging, objeto_con_fotos.id)
    assert item.photos[-1].color == 'azul'


def test_un_fichero_que_no_es_imagen_se_rechaza(auth_client, db, objeto_con_fotos):
    antes = len(objeto_con_fotos.photos)

    auth_client.post(f'/admin/pertenencias/{objeto_con_fotos.id}/fotos',
                     data={'photos': (io.BytesIO(b'no soy una imagen'), 'virus.exe')},
                     content_type='multipart/form-data')

    assert len(db.session.get(ResidentBelonging, objeto_con_fotos.id).photos) == antes
