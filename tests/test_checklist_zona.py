"""
test_checklist_zona.py — El checklist vuelve a salir, y depende de la zona.

Dos cosas a la vez.

La primera es una regresion: el checklist solo lo ofrecia `end_session`, el
boton "Finalizar" de la tarjeta. Cuando se puso el modo Solo NFC ese endpoint
empezo a devolver 403 y el boton dejo de dibujarse, asi que la pantalla quedo
inalcanzable sin que nadie tocara su codigo. El camino que se usa de verdad
—acercar el movil a la etiqueta— cerraba la limpieza directamente. Aqui se
comprueba desde ese camino, que es el que importa.

La segunda es que cada item vale para las zonas que se le marquen: lo que hay
que repasar en un bano no es lo que hay que repasar en una habitacion, pero
"luces apagadas" toca en todas. Un item sin ninguna zona marcada es uno
heredado de cuando la lista era unica, y no sale en ninguna parte hasta que
coordinacion le marca las suyas.
"""

from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import (AppSetting, ChecklistItem, CleaningRecord, Cleaner,
                        RoomType)


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def limpieza_abierta(db, cleaner_user, room):
    """Abierta hace diez minutos: ya ha pasado la duracion minima."""
    r = CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                       start_time=datetime.now() - timedelta(minutes=10))
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def otro_tipo_de_zona(db):
    rt = RoomType(name='Bano comun')
    db.session.add(rt)
    db.session.commit()
    return rt


def _item(db, texto, zonas, orden=0, activo=True):
    it = ChecklistItem(text=texto, sort_order=orden, active=activo,
                       room_types=list(zonas))
    db.session.add(it)
    db.session.commit()
    return it


def _escanear(client, headers, room, cleaner_user):
    return client.post('/api/nfc/scan', headers=headers,
                       json={'nfc_code': room.number, 'mode': 'cleaning',
                             'worker_id': cleaner_user.id})


# ── El escaneo NFC ofrece el checklist antes de cerrar ───────────────────────

def test_el_escaneo_ofrece_el_checklist_de_la_zona(
        client, db, limpieza_abierta, cleaner_user, room, room_type, worker_headers):
    _item(db, 'Vaciar la papelera', [room_type], orden=1)
    _item(db, 'Apagar la luz', [room_type], orden=0)

    res = _escanear(client, worker_headers, room, cleaner_user)

    assert res.status_code == 200
    data = res.get_json()
    assert data['action'] == 'select_checklist'
    assert data['record_id'] == limpieza_abierta.id
    # Ordenados por sort_order, que es como los coloca coordinacion.
    assert [i['text'] for i in data['items']] == ['Apagar la luz', 'Vaciar la papelera']


def test_el_escaneo_no_cierra_la_limpieza_al_ofrecer_el_checklist(
        client, db, limpieza_abierta, cleaner_user, room, room_type, worker_headers):
    """Cerrar y ademas preguntar dejaria el checklist sin poder guardarse."""
    _item(db, 'Reponer papel', [room_type])

    _escanear(client, worker_headers, room, cleaner_user)

    assert db.session.get(CleaningRecord, limpieza_abierta.id).end_time is None


def test_sin_items_en_la_zona_el_escaneo_cierra_como_siempre(
        client, db, limpieza_abierta, cleaner_user, room, worker_headers):
    """Las zonas sin checklist tienen que seguir cerrandose de un escaneo."""
    res = _escanear(client, worker_headers, room, cleaner_user)

    assert res.status_code == 200
    assert res.get_json()['action'] == 'ended'
    assert db.session.get(CleaningRecord, limpieza_abierta.id).end_time is not None


# ── Cada item, sus zonas ───────────────────────────────────────────────────────

def test_un_item_de_otra_zona_no_aparece(
        client, db, limpieza_abierta, cleaner_user, room, otro_tipo_de_zona,
        worker_headers):
    _item(db, 'Desinfectar los grifos', [otro_tipo_de_zona])

    res = _escanear(client, worker_headers, room, cleaner_user)

    assert res.get_json()['action'] == 'ended'


def test_un_item_sin_zona_no_aparece_en_ninguna(
        client, db, limpieza_abierta, cleaner_user, room, worker_headers):
    """Heredado de cuando la lista era unica: espera a que le marquen zonas."""
    _item(db, 'Item viejo sin zona', [])

    res = _escanear(client, worker_headers, room, cleaner_user)

    assert res.get_json()['action'] == 'ended'


def test_un_item_desactivado_no_aparece(
        client, db, limpieza_abierta, cleaner_user, room, room_type, worker_headers):
    _item(db, 'Item retirado', [room_type], activo=False)

    res = _escanear(client, worker_headers, room, cleaner_user)

    assert res.get_json()['action'] == 'ended'


def test_solo_salen_los_items_de_la_zona_escaneada(
        client, db, limpieza_abierta, cleaner_user, room, room_type,
        otro_tipo_de_zona, worker_headers):
    _item(db, 'Hacer la cama', [room_type])
    _item(db, 'Fregar el suelo del bano', [otro_tipo_de_zona])

    data = _escanear(client, worker_headers, room, cleaner_user).get_json()

    assert [i['text'] for i in data['items']] == ['Hacer la cama']


def test_un_item_de_varias_zonas_sale_en_todas(
        client, db, cleaner_user, room, second_room, room_type,
        otro_tipo_de_zona, worker_headers):
    """El caso que motivo el cambio: "luces apagadas" toca en todas partes."""
    second_room.room_type_id = otro_tipo_de_zona.id
    db.session.add_all([
        CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                       start_time=datetime.now() - timedelta(minutes=10)),
        CleaningRecord(cleaner_id=cleaner_user.id, room_id=second_room.id,
                       start_time=datetime.now() - timedelta(minutes=10)),
    ])
    db.session.commit()
    _item(db, 'Luces apagadas', [room_type, otro_tipo_de_zona])

    en_habitacion = _escanear(client, worker_headers, room, cleaner_user).get_json()
    en_bano = _escanear(client, worker_headers, second_room, cleaner_user).get_json()

    assert [i['text'] for i in en_habitacion['items']] == ['Luces apagadas']
    assert [i['text'] for i in en_bano['items']] == ['Luces apagadas']


def test_un_item_no_sale_en_una_zona_que_no_tiene_marcada(
        client, db, limpieza_abierta, cleaner_user, room, room_type,
        otro_tipo_de_zona, worker_headers):
    """Marcar varias zonas no es marcarlas todas."""
    _item(db, 'Papel WC', [otro_tipo_de_zona])
    _item(db, 'Hacer la cama', [room_type, otro_tipo_de_zona])

    data = _escanear(client, worker_headers, room, cleaner_user).get_json()

    assert [i['text'] for i in data['items']] == ['Hacer la cama']


# ── Confirmar el checklist cierra y guarda ───────────────────────────────────

def test_confirmar_el_checklist_cierra_y_guarda_lo_marcado(
        client, db, limpieza_abierta, cleaner_user, room, room_type, worker_headers):
    item = _item(db, 'Apagar la luz', [room_type])

    res = client.post('/api/nfc/finalize-cleaning', headers=worker_headers, json={
        'record_id': limpieza_abierta.id,
        'worker_id': cleaner_user.id,
        'checklist': [{'id': item.id, 'text': item.text, 'checked': True}],
    })

    assert res.status_code == 200
    guardado = db.session.get(CleaningRecord, limpieza_abierta.id)
    assert guardado.end_time is not None
    assert 'Apagar la luz' in guardado.checklist_json


def test_otra_trabajadora_no_puede_finalizar_la_limpieza_ajena(
        client, db, limpieza_abierta, cleaner_user, worker_headers):
    """_verify_worker_id: el worker_id que llega del cliente no vale por si solo."""
    otra = Cleaner(username='otra_limpiadora', name='Otra', role='limpieza')
    otra.set_password('x')
    db.session.add(otra)
    db.session.commit()

    res = client.post('/api/nfc/finalize-cleaning', headers=worker_headers, json={
        'record_id': limpieza_abierta.id,
        'worker_id': otra.id,
        'checklist': [],
    })

    assert res.status_code == 403
    assert db.session.get(CleaningRecord, limpieza_abierta.id).end_time is None


# ── El boton manual, cuando el modo Solo NFC esta desactivado ────────────────

def test_el_boton_manual_tambien_da_el_checklist_de_la_zona(
        client, db, limpieza_abierta, cleaner_user, room_type, worker_headers):
    AppSetting.set('nfc_only', 'false')
    _item(db, 'Cerrar la ventana', [room_type])
    try:
        res = client.post('/api/nfc/end-session', headers=worker_headers,
                          json={'record_id': limpieza_abierta.id,
                                'worker_id': cleaner_user.id, 'mode': 'cleaning'})
    finally:
        AppSetting.set('nfc_only', 'true')

    data = res.get_json()
    assert data['action'] == 'select_checklist'
    assert [i['text'] for i in data['items']] == ['Cerrar la ventana']


# ── Panel de administracion ──────────────────────────────────────────────────

def test_crear_un_item_sin_admin_no_se_permite(client, db, room_type):
    res = client.post('/checklist/add_edit',
                      data={'text': 'Colado', 'room_type_ids': room_type.id})

    assert res.status_code in (302, 401, 403)
    assert ChecklistItem.query.count() == 0


def test_el_formulario_sin_zona_no_crea_el_item(auth_client, db, room_type):
    res = auth_client.post('/checklist/add_edit',
                           data={'text': 'Sin zona', 'sort_order': '0'},
                           follow_redirects=True)

    assert res.status_code == 200
    assert ChecklistItem.query.count() == 0


def test_el_formulario_con_una_zona_inexistente_no_crea_el_item(auth_client, db):
    res = auth_client.post('/checklist/add_edit',
                           data={'text': 'Zona fantasma', 'room_type_ids': '9999'},
                           follow_redirects=True)

    assert res.status_code == 200
    assert ChecklistItem.query.count() == 0


def test_el_formulario_con_zona_crea_el_item(auth_client, db, room_type):
    auth_client.post('/checklist/add_edit',
                     data={'text': 'Ventilar la habitacion',
                           'sort_order': '2', 'room_type_ids': [room_type.id]},
                     follow_redirects=True)

    item = ChecklistItem.query.one()
    assert item.text == 'Ventilar la habitacion'
    assert [z.id for z in item.room_types] == [room_type.id]
    assert item.sort_order == 2


def test_el_formulario_guarda_varias_zonas(auth_client, db, room_type,
                                           otro_tipo_de_zona):
    auth_client.post('/checklist/add_edit',
                     data={'text': 'Luces apagadas', 'sort_order': '0',
                           'room_type_ids': [room_type.id, otro_tipo_de_zona.id]},
                     follow_redirects=True)

    item = ChecklistItem.query.one()
    assert sorted(z.id for z in item.room_types) == sorted([room_type.id,
                                                            otro_tipo_de_zona.id])


def test_editar_reemplaza_las_zonas_marcadas(auth_client, db, room_type,
                                             otro_tipo_de_zona):
    """Desmarcar una zona tiene que quitarla, no solo anadir las nuevas."""
    item = _item(db, 'Toallas dispuestas', [room_type, otro_tipo_de_zona])

    auth_client.post('/checklist/add_edit',
                     data={'item_id': item.id, 'text': 'Toallas dispuestas',
                           'sort_order': '0', 'room_type_ids': [otro_tipo_de_zona.id]},
                     follow_redirects=True)

    assert [z.id for z in ChecklistItem.query.one().room_types] == [otro_tipo_de_zona.id]
