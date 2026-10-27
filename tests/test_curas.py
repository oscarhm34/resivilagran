"""
test_curas.py — Curas: el episodio, sus partes de seguimiento y sus fotos.

Una cura es un episodio que dura dias o semanas: se abre cuando aparece la
herida y cada visita le anade un parte con fotos, hasta que cura. No es una
atencion abierta durante tres semanas, que el barrido de 24h cerraria con una
duracion falsa y sin tipo.

Heridas era el unico modulo del proyecto sin un solo test, asi que esto cubre
tambien lo que ya existia: la creacion, el historial y la validacion.
"""

import io

import pytest
from flask_jwt_extended import create_access_token
from PIL import Image

from app import db as _db
from app.models import (Cleaner, Resident, CareRecord, WoundRecord,
                        WoundUpdate, WoundPhoto)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def residente(db):
    r = Resident(name='Residente Cura', nfc_code='RES-CURA-1', active=True)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def otro_residente(db):
    r = Resident(name='Residente Dos', nfc_code='RES-CURA-2', active=True)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def worker_headers(app, cleaner_user):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


def _foto(nombre='cura.jpg', color=(200, 30, 30)):
    """Una imagen de verdad: el servidor la reprocesa con Pillow."""
    buf = io.BytesIO()
    Image.new('RGB', (60, 40), color).save(buf, 'JPEG')
    buf.seek(0)
    return (buf, nombre)


def _abrir_cura(client, headers, residente, **extra):
    datos = {'body_zone': 'sacrum', 'wound_type': 'ulcer',
             'severity': 'moderate', 'description': 'UPP grado 2'}
    datos.update(extra)
    return client.post(f'/api/worker/resident/{residente.id}/wounds',
                       json=datos, headers=headers)


# ── Autorización ─────────────────────────────────────────────────────────────

def test_sin_jwt_no_se_abre_una_cura(client, residente):
    resp = client.post(f'/api/worker/resident/{residente.id}/wounds',
                       json={'body_zone': 'sacrum', 'wound_type': 'ulcer'})
    assert resp.status_code == 401


def test_sin_jwt_no_se_anade_un_parte(client, db, residente, cleaner_user):
    herida = WoundRecord(resident_id=residente.id, body_zone='sacrum',
                         wound_type='ulcer', status='active')
    db.session.add(herida)
    db.session.commit()
    assert client.post(f'/api/worker/wound/{herida.id}/update',
                       json={'status': 'healing'}).status_code == 401


def test_sin_sesion_no_se_ve_el_historial(client, db, residente):
    herida = WoundRecord(resident_id=residente.id, body_zone='sacrum',
                         wound_type='ulcer', status='active')
    db.session.add(herida)
    db.session.commit()
    assert client.get(f'/api/wound/{herida.id}/history').status_code != 200


# ── Abrir la cura ────────────────────────────────────────────────────────────

def test_abrir_una_cura_crea_su_parte_de_apertura(client, db, worker_headers, residente):
    """El historial empieza el primer dia, no en el primer seguimiento."""
    resp = _abrir_cura(client, worker_headers, residente)
    assert resp.status_code == 201

    herida = db.session.get(WoundRecord, resp.get_json()['id'])
    assert herida.status == 'active'
    assert len(herida.updates) == 1
    assert herida.updates[0].description == 'UPP grado 2'


@pytest.mark.parametrize('campo,valor', [
    ('body_zone', 'la espalda'),
    ('wound_type', 'rozadura'),
    ('severity', 'gravisima'),
])
def test_un_valor_inventado_se_rechaza(client, worker_headers, residente, campo, valor):
    """Antes una zona desconocida se guardaba como 'other', que ni existe."""
    resp = _abrir_cura(client, worker_headers, residente, **{campo: valor})
    assert resp.status_code == 400
    assert 'no es valido' in resp.get_json()['error']


def test_sin_zona_no_se_abre(client, worker_headers, residente):
    resp = client.post(f'/api/worker/resident/{residente.id}/wounds',
                       json={'wound_type': 'ulcer'}, headers=worker_headers)
    assert resp.status_code == 400


# ── Partes de seguimiento ────────────────────────────────────────────────────

def test_el_historial_sale_completo_y_en_orden(client, db, worker_headers,
                                               auth_client, residente):
    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    for texto, estado in [('Va mejor', 'healing'), ('Casi cerrada', 'healing')]:
        resp = client.post(f'/api/worker/wound/{herida_id}/update',
                           json={'status': estado, 'description': texto},
                           headers=worker_headers)
        assert resp.status_code == 201

    historial = auth_client.get(f'/api/wound/{herida_id}/history').get_json()
    assert len(historial['updates']) == 3, 'apertura mas dos partes'
    # Del mas reciente al mas antiguo: el de apertura queda el ultimo.
    assert historial['updates'][-1]['description'] == 'UPP grado 2'
    assert historial['updates'][0]['description'] == 'Casi cerrada'


def test_el_parte_cambia_el_estado_de_la_cura(client, db, worker_headers, residente):
    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    client.post(f'/api/worker/wound/{herida_id}/update',
                json={'status': 'worsening'}, headers=worker_headers)

    db.session.expire_all()
    assert db.session.get(WoundRecord, herida_id).status == 'worsening'


def test_cerrar_la_cura_la_marca_curada(client, db, worker_headers, residente):
    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    client.post(f'/api/worker/wound/{herida_id}/update',
                json={'status': 'healed', 'description': 'Cerrada'},
                headers=worker_headers)

    db.session.expire_all()
    herida = db.session.get(WoundRecord, herida_id)
    assert herida.status == 'healed'
    assert herida.healed_at is not None


def test_reabrir_una_cura_borra_la_fecha_de_curacion(client, db, worker_headers, residente):
    """Una herida reabierta no puede seguir diciendo que curo el martes."""
    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    client.post(f'/api/worker/wound/{herida_id}/update',
                json={'status': 'healed'}, headers=worker_headers)
    client.post(f'/api/worker/wound/{herida_id}/update',
                json={'status': 'active'}, headers=worker_headers)

    db.session.expire_all()
    herida = db.session.get(WoundRecord, herida_id)
    assert herida.status == 'active'
    assert herida.healed_at is None


def test_un_estado_inventado_se_rechaza(client, worker_headers, residente):
    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    resp = client.post(f'/api/worker/wound/{herida_id}/update',
                       json={'status': 'regular'}, headers=worker_headers)
    assert resp.status_code == 400


def test_una_cura_que_no_existe_da_404(client, worker_headers):
    assert client.post('/api/worker/wound/9999/update',
                       json={'status': 'healing'},
                       headers=worker_headers).status_code == 404


# ── Fotos ────────────────────────────────────────────────────────────────────

def test_un_parte_con_fotos(client, db, worker_headers, residente):
    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    resp = client.post(
        f'/api/worker/wound/{herida_id}/update',
        data={'status': 'healing', 'description': 'Con fotos',
              'fotos': [_foto('a.jpg'), _foto('b.jpg', (30, 200, 30))]},
        headers=worker_headers, content_type='multipart/form-data')

    assert resp.status_code == 201
    assert len(resp.get_json()['fotos']) == 2
    parte = db.session.get(WoundUpdate, resp.get_json()['id'])
    assert [f.sort_order for f in parte.photos] == [0, 1]
    # Reprocesadas con Pillow: siempre salen JPEG, venga lo que venga.
    assert all(f.photo_path.endswith('.jpg') for f in parte.photos)


def test_mas_fotos_de_las_que_caben_se_rechazan(client, db, worker_headers, residente):
    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    resp = client.post(
        f'/api/worker/wound/{herida_id}/update',
        data={'status': 'healing', 'fotos': [_foto(f'{i}.jpg') for i in range(5)]},
        headers=worker_headers, content_type='multipart/form-data')

    assert resp.status_code == 400
    assert 'Como mucho' in resp.get_json()['error']
    assert WoundPhoto.query.count() == 0, 'y no queda ninguna a medias'


def test_un_formato_no_permitido_no_deja_fotos_sueltas(client, db, worker_headers,
                                                       residente):
    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    resp = client.post(
        f'/api/worker/wound/{herida_id}/update',
        data={'status': 'healing',
              'fotos': [_foto('buena.jpg'), _foto('mala.exe')]},
        headers=worker_headers, content_type='multipart/form-data')

    assert resp.status_code == 400
    assert WoundPhoto.query.count() == 0


# ── El enlace con la atención ────────────────────────────────────────────────

def test_el_parte_queda_atado_a_la_atencion(client, db, worker_headers,
                                            cleaner_user, residente):
    from datetime import datetime
    atencion = CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                          start_time=datetime.now())
    db.session.add(atencion)
    db.session.commit()

    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    resp = client.post(f'/api/worker/wound/{herida_id}/update',
                       json={'status': 'healing', 'care_record_id': atencion.id},
                       headers=worker_headers)

    parte = db.session.get(WoundUpdate, resp.get_json()['id'])
    assert parte.care_record_id == atencion.id
    assert len(atencion.wound_updates) == 1


def test_no_se_puede_colgar_un_parte_de_la_atencion_de_otra(client, db, worker_headers,
                                                            admin_user, residente):
    """Cruce de identidad: la atención tiene que ser de quien manda el parte."""
    from datetime import datetime
    ajena = CareRecord(worker_id=admin_user.id, resident_id=residente.id,
                       start_time=datetime.now())
    db.session.add(ajena)
    db.session.commit()

    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    resp = client.post(f'/api/worker/wound/{herida_id}/update',
                       json={'status': 'healing', 'care_record_id': ajena.id},
                       headers=worker_headers)
    assert resp.status_code == 403


def test_la_atencion_tiene_que_ser_del_mismo_residente(client, db, worker_headers,
                                                       cleaner_user, residente,
                                                       otro_residente):
    """La regresión de la herida anotada a quien no la tiene."""
    from datetime import datetime
    atencion = CareRecord(worker_id=cleaner_user.id, resident_id=otro_residente.id,
                          start_time=datetime.now())
    db.session.add(atencion)
    db.session.commit()

    herida_id = _abrir_cura(client, worker_headers, residente).get_json()['id']
    resp = client.post(f'/api/worker/wound/{herida_id}/update',
                       json={'status': 'healing', 'care_record_id': atencion.id},
                       headers=worker_headers)
    assert resp.status_code == 400


def test_un_parte_desde_el_panel_no_lleva_atencion(auth_client, db, residente,
                                                   admin_user):
    herida = WoundRecord(resident_id=residente.id, body_zone='sacrum',
                         wound_type='ulcer', status='active')
    db.session.add(herida)
    db.session.commit()

    assert auth_client.post(f'/api/wound/{herida.id}/update',
                            json={'status': 'healing'}).status_code == 200
    parte = WoundUpdate.query.filter_by(wound_id=herida.id).first()
    assert parte.care_record_id is None


# ── La lista que ve la trabajadora ───────────────────────────────────────────

def test_la_webapp_ve_las_curas_abiertas_y_no_las_curadas(client, worker_headers,
                                                          residente):
    abierta = _abrir_cura(client, worker_headers, residente).get_json()['id']
    cerrada = _abrir_cura(client, worker_headers, residente,
                          body_zone='left_heel').get_json()['id']
    client.post(f'/api/worker/wound/{cerrada}/update',
                json={'status': 'healed'}, headers=worker_headers)

    curas = client.get(f'/api/worker/resident/{residente.id}/wounds',
                       headers=worker_headers).get_json()['wounds']
    assert [c['id'] for c in curas] == [abierta]
    assert curas[0]['status_label'] == 'Activa'
    assert curas[0]['partes'] == 1
