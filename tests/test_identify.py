"""
test_identify.py — Identificar un objeto perdido por parecido con el inventario.

Lo que se protege aqui:

- Que la puntuacion no dependa de la luz. Es la propiedad que hizo falta
  arreglar: con el brillo dentro del histograma, la misma prenda fotografiada
  con otra luz se parecia a si misma un 0.20.
- Que solo se compare contra residentes de alta y contra descriptores de la
  version actual, que son los unicos comparables con el vector de la consulta.
- Que la foto de consulta no se guarde en ningun sitio.

El modelo ONNX no esta en todas las maquinas, asi que las pruebas de ruta
sustituyen `embed` por un vector deterministico derivado del color. El
histograma, que no necesita modelo, corre de verdad.
"""

import io
import os

import numpy as np
import pytest
from flask_jwt_extended import create_access_token
from PIL import Image

from app import image_match as im
from app.models import Resident, ResidentBelonging


# ── Utilidades ────────────────────────────────────────────────────────────────

def _img(color, size=(240, 240), brillo=1.0):
    a = np.full((size[1], size[0], 3), color, dtype=np.float32) * brillo
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def _jpeg(color, brillo=1.0):
    buf = io.BytesIO()
    _img(color, brillo=brillo).save(buf, 'JPEG', quality=90)
    buf.seek(0)
    return buf


def _fake_embed(img):
    """Vector deterministico a partir del color medio: mismo color, mismo vector."""
    medio = np.asarray(img.convert('RGB').resize((8, 8)), dtype=np.float32).mean(axis=(0, 1))
    v = np.zeros(im.EMBED_DIM, dtype=np.float32)
    v[:3] = medio / 255.0
    v[3] = 0.5                      # parte comun, para que nada de cero exacto
    return (v / np.linalg.norm(v)).astype(np.float32)


ROJO, AZUL, VERDE = (200, 40, 40), (40, 60, 200), (40, 170, 70)


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def con_modelo(monkeypatch):
    """Finge que el modelo esta y lo sustituye por el vector de color."""
    monkeypatch.setattr(im, 'model_available', lambda: True)
    monkeypatch.setattr(im, 'embed', _fake_embed)


@pytest.fixture
def uploads(app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'UPLOAD_FOLDER', str(tmp_path))
    return tmp_path


def _pertenencia(db, resident, color, descripcion, version=im.DESCRIPTOR_VERSION):
    img = _img(color)
    item = ResidentBelonging(
        resident_id=resident.id,
        photo_path=f'belongings/res_{resident.id}/{descripcion}.jpg',
        description=descripcion,
        embedding=im.to_bytes(_fake_embed(img)),
        color_hist=im.to_bytes(im.color_histogram(img)),
        descriptor_version=version,
    )
    db.session.add(item)
    db.session.commit()
    return item


@pytest.fixture
def antonia(db):
    r = Resident(name='Antonia Vidal', nfc_code='ID-1', active=True, room_number='101')
    db.session.add(r); db.session.commit(); return r


@pytest.fixture
def josep(db):
    r = Resident(name='Josep Roca', nfc_code='ID-2', active=True, room_number='102')
    db.session.add(r); db.session.commit(); return r


# ── El descriptor de color ────────────────────────────────────────────────────

def test_el_color_no_depende_de_la_luz():
    """La misma prenda con mas luz tiene que seguir siendo la misma prenda."""
    a = im.color_histogram(_img(ROJO))
    b = im.color_histogram(_img(ROJO, brillo=1.35))

    assert float(np.minimum(a, b).sum()) > 0.9


def test_el_color_distingue_dos_prendas_distintas():
    a = im.color_histogram(_img(ROJO))
    b = im.color_histogram(_img(AZUL))

    assert float(np.minimum(a, b).sum()) < 0.1


def test_el_histograma_suma_uno():
    assert float(im.color_histogram(_img(VERDE)).sum()) == pytest.approx(1.0, abs=1e-5)


# ── Puntuacion y bandas ───────────────────────────────────────────────────────

def test_la_banda_sigue_los_umbrales():
    assert im.band(im.BAND_HIGH) == 'alta'
    assert im.band(im.BAND_HIGH - 0.01) == 'media'
    assert im.band(im.BAND_MEDIUM) == 'media'
    assert im.band(im.BAND_MEDIUM - 0.01) == 'baja'


def test_sin_embedding_no_hay_puntuacion():
    """Una pertenencia sin describir no puede colarse como candidata."""
    h = im.color_histogram(_img(ROJO))
    assert im.score(None, h, _fake_embed(_img(ROJO)), h) is None
    assert im.score(_fake_embed(_img(ROJO)), h, None, h) is None


def test_el_orden_respeta_suelo_y_limite():
    q_emb, q_hist = _fake_embed(_img(ROJO)), im.color_histogram(_img(ROJO))
    candidatos = [
        ('igual', _fake_embed(_img(ROJO)), im.color_histogram(_img(ROJO))),
        ('otro', _fake_embed(_img(AZUL)), im.color_histogram(_img(AZUL))),
    ]

    salida = im.rank(q_emb, q_hist, candidatos)

    assert [fila[0] for fila in salida] == ['igual']       # el azul cae bajo el suelo
    assert salida[0][2] == 'alta'
    assert im.rank(q_emb, q_hist, candidatos, floor=0.0, limit=1) == salida[:1]


def test_los_vectores_van_y_vuelven_de_bytes():
    v = _fake_embed(_img(ROJO))
    assert np.allclose(im.from_bytes(im.to_bytes(v), im.EMBED_DIM), v)
    assert im.to_bytes(None) is None
    assert im.from_bytes(None, im.EMBED_DIM) is None
    assert im.from_bytes(im.to_bytes(v), im.EMBED_DIM + 1) is None   # dimension que no cuadra


# ── La ruta ───────────────────────────────────────────────────────────────────

def test_sin_token_devuelve_401(client, db):
    assert client.post('/api/worker/belongings/identify').status_code == 401


def test_sin_foto_devuelve_400(client, db, worker_headers, con_modelo):
    res = client.post('/api/worker/belongings/identify', headers=worker_headers, data={})

    assert res.status_code == 400
    assert 'foto' in res.get_json()['error'].lower()


def test_sin_modelo_la_identificacion_se_apaga(client, db, worker_headers, monkeypatch):
    """Mejor apagada que dando resultados calladamente peores."""
    monkeypatch.setattr(im, 'model_available', lambda: False)

    res = client.post('/api/worker/belongings/identify', headers=worker_headers,
                      data={'photo': (_jpeg(ROJO), 'perdido.jpg')},
                      content_type='multipart/form-data')

    assert res.status_code == 503


def test_un_ejecutable_disfrazado_se_rechaza(client, db, worker_headers, con_modelo):
    res = client.post('/api/worker/belongings/identify', headers=worker_headers,
                      data={'photo': (io.BytesIO(b'MZ\x90\x00'), 'virus.exe')},
                      content_type='multipart/form-data')

    assert res.status_code == 400


def test_devuelve_al_dueno_correcto_primero(
        client, db, worker_headers, con_modelo, antonia, josep):
    _pertenencia(db, antonia, ROJO, 'jersey rojo')
    _pertenencia(db, josep, AZUL, 'jersey azul')

    datos = client.post('/api/worker/belongings/identify', headers=worker_headers,
                        data={'photo': (_jpeg(ROJO), 'perdido.jpg')},
                        content_type='multipart/form-data').get_json()

    assert datos['comparadas'] == 2
    assert datos['matches'][0]['resident_name'] == 'Antonia Vidal'
    assert datos['matches'][0]['room_number'] == '101'
    assert datos['matches'][0]['band'] == 'alta'
    assert 'Josep Roca' not in [m['resident_name'] for m in datos['matches']]


def test_la_misma_prenda_con_otra_luz_sigue_saliendo(
        client, db, worker_headers, con_modelo, antonia):
    _pertenencia(db, antonia, ROJO, 'jersey rojo')

    datos = client.post('/api/worker/belongings/identify', headers=worker_headers,
                        data={'photo': (_jpeg(ROJO, brillo=1.3), 'perdido.jpg')},
                        content_type='multipart/form-data').get_json()

    assert datos['matches'], 'la luz no puede hacer desaparecer la prenda'
    assert datos['matches'][0]['resident_name'] == 'Antonia Vidal'


def test_no_se_compara_con_residentes_de_baja(
        client, db, worker_headers, con_modelo, antonia, josep):
    _pertenencia(db, antonia, ROJO, 'jersey rojo')
    antonia.active = False
    db.session.commit()

    datos = client.post('/api/worker/belongings/identify', headers=worker_headers,
                        data={'photo': (_jpeg(ROJO), 'perdido.jpg')},
                        content_type='multipart/form-data').get_json()

    assert datos['comparadas'] == 0
    assert datos['matches'] == []


def test_se_ignoran_los_descriptores_de_otra_version(
        client, db, worker_headers, con_modelo, antonia):
    """Un vector calculado de otra forma no es comparable con este."""
    _pertenencia(db, antonia, ROJO, 'jersey rojo', version='version-vieja')

    datos = client.post('/api/worker/belongings/identify', headers=worker_headers,
                        data={'photo': (_jpeg(ROJO), 'perdido.jpg')},
                        content_type='multipart/form-data').get_json()

    assert datos['comparadas'] == 0


def test_la_foto_de_consulta_no_se_guarda(
        client, db, worker_headers, con_modelo, antonia, uploads):
    _pertenencia(db, antonia, ROJO, 'jersey rojo')

    client.post('/api/worker/belongings/identify', headers=worker_headers,
                data={'photo': (_jpeg(ROJO), 'perdido.jpg')},
                content_type='multipart/form-data')

    escritos = [f for _, _, fs in os.walk(str(uploads)) for f in fs]
    assert escritos == [], 'la foto del objeto perdido no debe quedarse en disco'
