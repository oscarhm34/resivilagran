"""Orientación EXIF de las fotos subidas y giro manual de las ya guardadas."""
import io
import os

import pytest
from PIL import Image

from app.models import Resident
from app.utils import _open_image_oriented


@pytest.fixture(autouse=True)
def uploads_tmp(app, tmp_path):
    """Aisla las subidas en un directorio temporal: los tests no tocan uploads/."""
    original = app.config['UPLOAD_FOLDER']
    app.config['UPLOAD_FOLDER'] = str(tmp_path)
    yield
    app.config['UPLOAD_FOLDER'] = original


def _jpeg_with_orientation(size, orientation=None):
    """Crea un JPEG en memoria, opcionalmente con etiqueta EXIF Orientation."""
    img = Image.new('RGB', size, 'red')
    buf = io.BytesIO()
    if orientation is None:
        img.save(buf, 'JPEG')
    else:
        exif = img.getexif()
        exif[274] = orientation  # 274 = Orientation
        img.save(buf, 'JPEG', exif=exif.tobytes())
    buf.seek(0)
    return buf


# ── El bug: fotos de móvil giradas 90º ───────────────────────────────────────

def test_open_image_oriented_aplica_la_rotacion_exif():
    """Orientation=6 significa 'girar 90º': el ancho y el alto deben cambiarse."""
    source = _jpeg_with_orientation((200, 100), orientation=6)

    img = _open_image_oriented(source)

    assert img.size == (100, 200)


def test_open_image_oriented_no_toca_una_imagen_sin_exif():
    source = _jpeg_with_orientation((200, 100))

    img = _open_image_oriented(source)

    assert img.size == (200, 100)


def test_foto_de_residente_se_guarda_con_la_orientacion_corregida(auth_client, db, app):
    photo = _jpeg_with_orientation((200, 100), orientation=6)

    auth_client.post('/residents/add_edit', data={
        'name': 'Residente Girado',
        'nfc_code': 'NFC-ROT-1',
        'photo': (photo, 'foto.jpg'),
    }, content_type='multipart/form-data', follow_redirects=True)

    r = Resident.query.filter_by(nfc_code='NFC-ROT-1').first()
    assert r is not None and r.photo_path
    saved = os.path.join(app.config['UPLOAD_FOLDER'], r.photo_path)
    assert Image.open(saved).size == (100, 200)


# ── Giro manual de las fotos antiguas ────────────────────────────────────────

def test_rotar_foto_gira_el_fichero_en_disco(auth_client, db, app):
    photo = _jpeg_with_orientation((200, 100))
    auth_client.post('/residents/add_edit', data={
        'name': 'Residente Recto',
        'nfc_code': 'NFC-ROT-2',
        'photo': (photo, 'foto.jpg'),
    }, content_type='multipart/form-data', follow_redirects=True)
    r = Resident.query.filter_by(nfc_code='NFC-ROT-2').first()
    saved = os.path.join(app.config['UPLOAD_FOLDER'], r.photo_path)
    assert Image.open(saved).size == (200, 100)

    res = auth_client.post(f'/residents/{r.id}/rotate-photo', data={'direction': 'cw'})

    assert res.status_code == 200
    assert res.get_json()['ok'] is True
    assert Image.open(saved).size == (100, 200)


def test_rotar_foto_rechaza_un_sentido_no_valido(auth_client, db, app):
    photo = _jpeg_with_orientation((200, 100))
    auth_client.post('/residents/add_edit', data={
        'name': 'Residente Recto 2',
        'nfc_code': 'NFC-ROT-3',
        'photo': (photo, 'foto.jpg'),
    }, content_type='multipart/form-data', follow_redirects=True)
    r = Resident.query.filter_by(nfc_code='NFC-ROT-3').first()

    res = auth_client.post(f'/residents/{r.id}/rotate-photo', data={'direction': 'flip'})

    assert res.status_code == 400


def test_rotar_foto_de_residente_sin_foto_devuelve_404(auth_client, db):
    r = Resident(name='Sin Foto', nfc_code='NFC-ROT-4')
    db.session.add(r)
    db.session.commit()

    res = auth_client.post(f'/residents/{r.id}/rotate-photo', data={'direction': 'cw'})

    assert res.status_code == 404


def test_rotar_foto_requiere_admin(client, db):
    r = Resident(name='Sin Sesion', nfc_code='NFC-ROT-5')
    db.session.add(r)
    db.session.commit()

    res = client.post(f'/residents/{r.id}/rotate-photo', data={'direction': 'cw'})

    assert res.status_code in (302, 401, 403)
