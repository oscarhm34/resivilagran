"""
test_shift_board.py — El tablero de turnos: puestos, fichas y publicacion.

La rejilla mensual dice cuantas horas lleva cada una; el tablero dice quien hace
M1 el martes, que es la decision que se toma al planificar. Para eso hace falta
el concepto de puesto, que hasta ahora no existia: el unico eje era el tramo
horario.

Lo que se protege aqui es lo que puede corromper un cuadrante al arrastrar:
que una persona no acabe en dos sitios el mismo dia, que dos personas no ocupen
la misma casilla, que nadie quede asignado estando de baja, y que lo que se
coloca a mano no se lo lleve por delante la siguiente generacion automatica.
"""

import json
import re
from datetime import date, datetime, time, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import (
    Absence, AbsenceType, AppSetting, AuditLog, Cleaner, Notification,
    ShiftAssignment, ShiftPosition, ShiftType, ShiftWeekPublication,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

LUNES = date(2026, 10, 5)


@pytest.fixture
def manana(db):
    st = ShiftType(name='Manana', short_name='M', color='#8ab4e8',
                   start_time=time(7, 30), end_time=time(14, 0),
                   sort_order=1, active=True, board_row='manana')
    db.session.add(st)
    db.session.commit()
    return st


@pytest.fixture
def noche_c(db):
    """La que entra por la noche y sale por la manana del dia siguiente."""
    st = ShiftType(name='Noche C', short_name='NC', color='#5c6670',
                   start_time=time(22, 0), end_time=time(7, 0),
                   sort_order=9, active=True, board_row='noche')
    db.session.add(st)
    db.session.commit()
    return st


@pytest.fixture
def tarde(db):
    st = ShiftType(name='Tarde', short_name='T', color='#8fe3b0',
                   start_time=time(14, 0), end_time=time(20, 30),
                   sort_order=2, active=True, board_row='tarde')
    db.session.add(st)
    db.session.commit()
    return st


@pytest.fixture
def cocina(db):
    """Un puesto de fuera de planta: de serie se dibuja a un lado del recuadro."""
    st = ShiftType(name='Cocina', short_name='CO', color='#f0a01e',
                   start_time=time(9, 0), end_time=time(16, 0),
                   sort_order=5, active=True, board_row='lateral')
    db.session.add(st)
    db.session.commit()
    return st


@pytest.fixture
def recepcion(db):
    """Otro puesto de fuera, y su horario se pisa con el de cocina."""
    st = ShiftType(name='Recepcion', short_name='RE', color='#b9a7e8',
                   start_time=time(8, 0), end_time=time(15, 0),
                   sort_order=6, active=True, board_row='lateral')
    db.session.add(st)
    db.session.commit()
    return st


def _puesto(db, shift_type, code, orden=0, echo=None, echo_ayer=False):
    p = ShiftPosition(shift_type_id=shift_type.id, code=code, sort_order=orden,
                      echo_row=echo, echo_previous_day=echo_ayer)
    db.session.add(p)
    db.session.commit()
    return p


def _trabajadora(db, nombre, usuario):
    c = Cleaner(username=usuario, name=nombre, is_admin=False, active=True,
                role='atenciones')
    c.set_password('x123456')
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture
def ana(db):
    return _trabajadora(db, 'Ana Pons', 'ana')


@pytest.fixture
def berta(db):
    return _trabajadora(db, 'Berta Lluch', 'berta')


def _asignar(client, dia, puesto, trabajadora, desde=None):
    """Soltar una ficha. `desde` es la casilla de la que viene, si venia de una."""
    cuerpo = {'date': dia.isoformat(), 'position_id': puesto.id,
              'cleaner_id': trabajadora.id}
    if desde is not None:
        cuerpo['from_position_id'] = desde.id
    return client.post('/cuadrantes/tablero/asignar', json=cuerpo)


# ── Autorizacion ──────────────────────────────────────────────────────────────

RUTAS_GET = [
    '/cuadrantes/tablero',
    '/cuadrantes/tablero/dia/2026-10-05',
    '/cuadrantes/tablero/envios',
    '/cuadrantes/puestos',
]
RUTAS_POST = [
    '/cuadrantes/tablero/asignar',
    '/cuadrantes/tablero/quitar',
    '/cuadrantes/tablero/publicar',
    '/cuadrantes/tablero/anchos',
    '/cuadrantes/tablero/orden',
    '/cuadrantes/puestos/add_edit',
    '/cuadrantes/puestos/delete/1',
]


@pytest.mark.parametrize('ruta', RUTAS_GET)
def test_lecturas_sin_sesion_redirigen_al_login(client, db, ruta):
    res = client.get(ruta)
    assert res.status_code in (301, 302)
    assert '/admin/login' in res.headers['Location']


@pytest.mark.parametrize('ruta', RUTAS_POST)
def test_escrituras_sin_sesion_redirigen_al_login(client, db, ruta):
    res = client.post(ruta, json={})
    assert res.status_code in (301, 302)
    assert '/admin/login' in res.headers['Location']


@pytest.mark.parametrize('ruta', RUTAS_GET + RUTAS_POST)
def test_trabajadora_sin_admin_no_puede(client, db, cleaner_user, ruta):
    client.post('/admin/login',
                data={'username': 'limpiadora1', 'password': 'limpia123'},
                follow_redirects=True)
    res = client.post(ruta, json={}) if ruta in RUTAS_POST else client.get(ruta)
    assert res.status_code in (302, 403)


# ── Colocar una ficha ─────────────────────────────────────────────────────────

def test_colocar_crea_la_asignacion_con_su_puesto(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')

    res = _asignar(auth_client, LUNES, m1, ana)

    assert res.status_code == 200
    a = ShiftAssignment.query.filter_by(cleaner_id=ana.id, date=LUNES).one()
    assert a.position_id == m1.id
    assert a.shift_type_id == manana.id
    assert a.source == 'manual'
    assert a.updated_at is not None


def test_lo_colocado_a_mano_es_siempre_override(auth_client, db, manana, ana):
    """Si no, la siguiente generacion automatica del mes se lo lleva."""
    m1 = _puesto(db, manana, 'M1')

    _asignar(auth_client, LUNES, m1, ana)

    assert ShiftAssignment.query.filter_by(cleaner_id=ana.id).one().is_override is True


def test_arrastrar_de_una_casilla_a_otra_mueve(auth_client, db, manana, ana):
    """Viene de M1, asi que es la misma fila cambiando de puesto."""
    m1 = _puesto(db, manana, 'M1', 1)
    m2 = _puesto(db, manana, 'M2', 2)
    _asignar(auth_client, LUNES, m1, ana)

    res = _asignar(auth_client, LUNES, m2, ana, desde=m1)

    assert res.status_code == 200
    filas = ShiftAssignment.query.filter_by(cleaner_id=ana.id, date=LUNES).all()
    assert len(filas) == 1
    assert filas[0].position_id == m2.id


def test_arrastrar_de_la_lista_anade_un_segundo_puesto(auth_client, db, manana, tarde, ana):
    """Doblar un M1 con un T1 pasa cuando falta alguien, y hay que apuntarlo."""
    m1 = _puesto(db, manana, 'M1')
    t1 = _puesto(db, tarde, 'T1')
    _asignar(auth_client, LUNES, m1, ana)

    res = _asignar(auth_client, LUNES, t1, ana)      # sin `desde`: viene de la lista

    assert res.status_code == 200
    puestos = {a.position.code for a in
               ShiftAssignment.query.filter_by(cleaner_id=ana.id, date=LUNES).all()}
    assert puestos == {'M1', 'T1'}


def test_quitar_un_puesto_deja_el_otro(auth_client, db, manana, tarde, ana):
    m1 = _puesto(db, manana, 'M1')
    t1 = _puesto(db, tarde, 'T1')
    _asignar(auth_client, LUNES, m1, ana)
    _asignar(auth_client, LUNES, t1, ana)

    auth_client.post('/cuadrantes/tablero/quitar', json={
        'date': LUNES.isoformat(), 'cleaner_id': ana.id, 'position_id': m1.id})

    filas = ShiftAssignment.query.filter_by(cleaner_id=ana.id, date=LUNES).all()
    assert len(filas) == 1
    assert filas[0].position_id == t1.id


def test_la_respuesta_dice_que_lleva_puesto_cada_una(auth_client, db, manana, tarde, ana):
    m1 = _puesto(db, manana, 'M1')
    t1 = _puesto(db, tarde, 'T1')
    _asignar(auth_client, LUNES, m1, ana)

    res = _asignar(auth_client, LUNES, t1, ana)

    assert sorted(res.get_json()['puestos'][str(ana.id)]) == ['M1', 'T1']


def test_soltar_sobre_una_casilla_ocupada_desplaza_a_quien_estaba(
        auth_client, db, manana, ana, berta):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    res = _asignar(auth_client, LUNES, m1, berta)

    assert res.status_code == 200
    assert res.get_json()['desplazada']['cleaner_id'] == ana.id
    assert ShiftAssignment.query.filter_by(date=LUNES, position_id=m1.id).one().cleaner_id == berta.id
    assert ShiftAssignment.query.filter_by(cleaner_id=ana.id, date=LUNES).first() is None


def test_colocar_a_quien_ya_estaba_ahi_no_rompe_nada(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    res = _asignar(auth_client, LUNES, m1, ana)

    assert res.status_code == 200
    assert res.get_json()['desplazada'] is None
    assert ShiftAssignment.query.filter_by(date=LUNES).count() == 1


def test_una_persona_de_baja_no_se_puede_colocar(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    tipo = AbsenceType(name='Baja medica', short_name='BAJ', color='#cf222e')
    db.session.add(tipo)
    db.session.flush()
    db.session.add(Absence(cleaner_id=ana.id, absence_type_id=tipo.id,
                           start_date=LUNES - timedelta(days=1),
                           end_date=LUNES + timedelta(days=3)))
    db.session.commit()

    res = _asignar(auth_client, LUNES, m1, ana)

    assert res.status_code == 400
    assert 'baja' in res.get_json()['error']
    assert ShiftAssignment.query.count() == 0


def test_colocar_en_un_puesto_que_no_existe_da_404(auth_client, db, ana):
    res = auth_client.post('/cuadrantes/tablero/asignar', json={
        'date': LUNES.isoformat(), 'position_id': 9999, 'cleaner_id': ana.id})
    assert res.status_code == 404


def test_colocar_sin_datos_se_rechaza(auth_client, db):
    assert auth_client.post('/cuadrantes/tablero/asignar', json={}).status_code == 400


def test_colocar_deja_rastro_en_la_auditoria(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')

    _asignar(auth_client, LUNES, m1, ana)

    registro = AuditLog.query.filter_by(table_name='shift_assignment').first()
    assert registro is not None
    assert 'M1' in registro.details


# ── Quitar ────────────────────────────────────────────────────────────────────

def test_quitar_libera_a_la_persona(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    res = auth_client.post('/cuadrantes/tablero/quitar', json={
        'date': LUNES.isoformat(), 'cleaner_id': ana.id})

    assert res.status_code == 200
    assert ShiftAssignment.query.count() == 0


def test_quitar_a_quien_no_estaba_no_falla(auth_client, db, ana):
    res = auth_client.post('/cuadrantes/tablero/quitar', json={
        'date': LUNES.isoformat(), 'cleaner_id': ana.id})
    assert res.status_code == 200


# ── El recuadro ───────────────────────────────────────────────────────────────

def test_el_recuadro_del_dia_ensena_a_quien_lo_ocupa(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    texto = auth_client.get(f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True)

    assert 'M1' in texto
    assert 'Ana Pons' in texto


def test_la_lista_lleva_a_todo_el_personal(auth_client, db, manana, ana, berta):
    """Tambien a quien ya tiene puesto: es la unica forma de poder doblarla."""
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    lista = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True).split('id="tb-banquillo"')[1]

    assert 'Berta Lluch' in lista
    assert 'Ana Pons' in lista


def test_la_lista_dice_que_puesto_lleva_ya_cada_una(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    lista = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True).split('id="tb-banquillo"')[1]

    assert 'tb-tag">M1<' in lista


def test_la_noche_se_refleja_en_la_manana_del_dia_siguiente(
        auth_client, db, manana, noche_c, ana):
    """Quien hizo la noche sigue aqui media manana: su reflejo sale en D+1."""
    nc = _puesto(db, noche_c, 'NIT C', echo='manana', echo_ayer=True)
    _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, nc, ana)

    martes = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES + timedelta(days=1)}').get_data(as_text=True)

    # El reflejo del martes la ensena, y lleva la marca del dia anterior.
    assert 'NIT C dia-1' in martes
    assert 'Ana Pons' in martes.split('id="tb-banquillo"')[0]


def test_el_reflejo_del_mismo_dia_no_mira_a_la_vispera(
        auth_client, db, manana, noche_c, ana):
    """Quien entra por la tarde esta esa misma tarde, no la del dia anterior."""
    na = _puesto(db, noche_c, 'NIT A', echo='tarde')
    _asignar(auth_client, LUNES, na, ana)

    lunes = auth_client.get(f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True)
    martes = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES + timedelta(days=1)}').get_data(as_text=True)

    assert 'Ana Pons' in lunes.split('id="tb-banquillo"')[0]
    assert 'Ana Pons' not in martes.split('id="tb-banquillo"')[0]


def test_quien_ocupa_un_puesto_sale_tambien_en_su_reflejo(
        auth_client, db, noche_c, ana):
    """El reflejo es la misma asignacion, asi que la ficha sale en los dos.

    Contar las veces y no solo mirar si aparece: con un `in` a secas, un reflejo
    vacio pasa desapercibido porque la casilla de noche ya la ensena.
    """
    na = _puesto(db, noche_c, 'NIT A', echo='tarde')
    _asignar(auth_client, LUNES, na, ana)

    recuadro = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True).split('id="tb-banquillo"')[0]

    # Dos cajas, y el nombre va en el aria-label y en el texto de cada una.
    assert recuadro.count('Ana Pons') == 4


def test_sin_reflejo_la_ficha_sale_una_sola_vez(auth_client, db, noche_c, ana):
    nb = _puesto(db, noche_c, 'NIT B')

    recuadro_antes = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True)
    _asignar(auth_client, LUNES, nb, ana)
    recuadro = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True).split('id="tb-banquillo"')[0]

    assert 'NIT B' in recuadro_antes
    assert recuadro.count('Ana Pons') == 2


def test_el_puesto_de_noche_se_sigue_editando_en_su_casilla(
        auth_client, db, noche_c, ana):
    """El reflejo no es un puesto nuevo: la asignacion es una sola."""
    nc = _puesto(db, noche_c, 'NIT C', echo='manana', echo_ayer=True)

    res = _asignar(auth_client, LUNES, nc, ana)

    assert res.status_code == 200
    assert ShiftAssignment.query.filter_by(position_id=nc.id).count() == 1


def test_el_reflejo_no_cuenta_como_un_puesto_mas_en_la_lista(
        auth_client, db, noche_c, ana):
    """NIT A sale dos veces en el recuadro, pero es un solo puesto."""
    nc = _puesto(db, noche_c, 'NIT A', echo='tarde')
    _asignar(auth_client, LUNES, nc, ana)

    lista = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True).split('id="tb-banquillo"')[1]

    assert lista.count('tb-tag">NIT A<') == 1


def test_un_turno_sin_fila_no_sale_en_el_tablero(auth_client, db, ana):
    suelto = ShiftType(name='Refuerzo suelto', short_name='RS', color='#f0a01e',
                       start_time=time(9, 0), end_time=time(13, 0), active=True)
    db.session.add(suelto)
    db.session.commit()
    _puesto(db, suelto, 'RS1')

    texto = auth_client.get(f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True)

    assert 'RS1' not in texto


def test_la_semana_ensena_los_siete_dias(auth_client, db, manana, ana):
    _puesto(db, manana, 'M1')

    texto = auth_client.get(f'/cuadrantes/tablero?semana={LUNES}').get_data(as_text=True)

    assert '05/10' in texto and '11/10' in texto


def test_sin_puestos_el_tablero_lo_dice(auth_client, db):
    texto = auth_client.get('/cuadrantes/tablero').get_data(as_text=True)
    assert 'no hay puestos definidos' in texto


# ── Publicar ──────────────────────────────────────────────────────────────────

def test_publicar_guarda_la_semana_y_avisa(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    res = auth_client.post('/cuadrantes/tablero/publicar',
                           json={'week_start': LUNES.isoformat()})

    assert res.status_code == 200
    assert res.get_json()['avisadas'] == 1
    pub = ShiftWeekPublication.query.filter_by(week_start=LUNES).one()
    assert pub.published_at is not None
    assert Notification.query.filter_by(worker_id=ana.id, type='shift_published').count() == 1


def test_publicar_acepta_cualquier_dia_de_la_semana(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    auth_client.post('/cuadrantes/tablero/publicar',
                     json={'week_start': (LUNES + timedelta(days=3)).isoformat()})

    assert ShiftWeekPublication.query.one().week_start == LUNES


def test_republicar_no_duplica_la_fila(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)
    auth_client.post('/cuadrantes/tablero/publicar', json={'week_start': LUNES.isoformat()})

    auth_client.post('/cuadrantes/tablero/publicar', json={'week_start': LUNES.isoformat()})

    assert ShiftWeekPublication.query.count() == 1


def test_republicar_sin_cambios_no_avisa_a_nadie(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)
    auth_client.post('/cuadrantes/tablero/publicar', json={'week_start': LUNES.isoformat()})
    Notification.query.delete()
    db.session.commit()

    res = auth_client.post('/cuadrantes/tablero/publicar',
                           json={'week_start': LUNES.isoformat()})

    assert res.get_json()['avisadas'] == 0
    assert Notification.query.count() == 0


def test_un_cambio_despues_de_publicar_marca_la_semana(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)
    auth_client.post('/cuadrantes/tablero/publicar', json={'week_start': LUNES.isoformat()})
    # Se adelanta el reloj de la asignacion: es lo que hace una baja de ultima hora.
    a = ShiftAssignment.query.one()
    a.updated_at = datetime.now() + timedelta(minutes=5)
    db.session.commit()

    texto = auth_client.get(f'/cuadrantes/tablero?semana={LUNES}').get_data(as_text=True)

    assert 'Modificada despues de publicar' in texto


def test_publicar_una_semana_mal_formada_se_rechaza(auth_client, db):
    assert auth_client.post('/cuadrantes/tablero/publicar',
                            json={'week_start': 'ayer'}).status_code == 400


# ── Envios ────────────────────────────────────────────────────────────────────

def test_el_horario_de_cada_una_solo_lleva_sus_dias(auth_client, db, manana, ana, berta):
    """El texto del aviso, probado contra la funcion y no contra la pantalla."""
    from app.blueprints.shifts import _horario_de

    m1 = _puesto(db, manana, 'M1', 1)
    m2 = _puesto(db, manana, 'M2', 2)
    _asignar(auth_client, LUNES, m1, ana)
    _asignar(auth_client, LUNES + timedelta(days=1), m2, berta)

    lineas = _horario_de(ana.id, LUNES)
    assert 'Lun 05/10: Manana' in lineas[0]
    assert 'M1' in lineas[0]
    # El dia que trabaja Berta, Ana lo tiene libre: cada una ve lo suyo.
    assert lineas[1] == 'Mar 06/10: Libre'
    assert len(lineas) == 7


def test_sin_telefono_la_pantalla_lo_dice(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    texto = auth_client.get(
        f'/cuadrantes/tablero/envios?semana={LUNES}').get_data(as_text=True)

    assert 'Sin telefono' in texto
    assert 'wa.me' not in texto


def test_con_telefono_queda_el_respaldo_de_abrir_whatsapp_a_mano(auth_client, db,
                                                                 manana, ana):
    ana.phone = '+34 600 11 22 33'
    db.session.commit()
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)

    texto = auth_client.get(
        f'/cuadrantes/tablero/envios?semana={LUNES}').get_data(as_text=True)

    # El enlace de siempre, con el numero ya en el formato que entiende WhatsApp.
    assert 'https://wa.me/34600112233?text=' in texto
    assert 'Ana' in texto


# ── Puestos ───────────────────────────────────────────────────────────────────

def test_crear_un_puesto(auth_client, db, manana):
    auth_client.post('/cuadrantes/puestos/add_edit', data={
        'shift_type_id': manana.id, 'code': 'm1', 'name': 'Primera de manana',
        'sort_order': '1', 'active': 'on'}, follow_redirects=True)

    p = ShiftPosition.query.one()
    assert p.code == 'M1'           # se guarda en mayusculas
    assert p.name == 'Primera de manana'
    assert p.echo_row is None


def test_un_puesto_puede_reflejarse_en_otra_fila(auth_client, db, noche_c):
    auth_client.post('/cuadrantes/puestos/add_edit', data={
        'shift_type_id': noche_c.id, 'code': 'NIT C', 'echo_row': 'manana',
        'echo_previous_day': 'on', 'active': 'on'}, follow_redirects=True)

    p = ShiftPosition.query.one()
    assert p.echo_row == 'manana'
    assert p.echo_previous_day is True


def test_el_reflejo_en_una_fila_inventada_se_ignora(auth_client, db, noche_c):
    auth_client.post('/cuadrantes/puestos/add_edit', data={
        'shift_type_id': noche_c.id, 'code': 'NIT C', 'echo_row': 'madrugada',
        'echo_previous_day': 'on', 'active': 'on'}, follow_redirects=True)

    p = ShiftPosition.query.one()
    assert p.echo_row is None
    assert p.echo_previous_day is False


def test_no_se_repite_el_codigo_dentro_del_mismo_turno(auth_client, db, manana):
    _puesto(db, manana, 'M1')

    auth_client.post('/cuadrantes/puestos/add_edit', data={
        'shift_type_id': manana.id, 'code': 'm1', 'active': 'on'},
        follow_redirects=True)

    assert ShiftPosition.query.count() == 1


def test_un_puesto_sin_codigo_no_se_crea(auth_client, db, manana):
    auth_client.post('/cuadrantes/puestos/add_edit', data={
        'shift_type_id': manana.id, 'code': '  ', 'active': 'on'},
        follow_redirects=True)

    assert ShiftPosition.query.count() == 0


def test_borrar_un_puesto_sin_usar(auth_client, db, manana):
    p = _puesto(db, manana, 'M1')

    auth_client.post(f'/cuadrantes/puestos/delete/{p.id}', follow_redirects=True)

    assert ShiftPosition.query.count() == 0


def test_no_se_borra_un_puesto_que_ya_se_ha_usado(auth_client, db, manana, ana):
    p = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, p, ana)

    res = auth_client.post(f'/cuadrantes/puestos/delete/{p.id}', follow_redirects=True)

    assert 'No se puede borrar' in res.get_data(as_text=True)
    assert ShiftPosition.query.count() == 1


# ── La rejilla mensual sigue entera ───────────────────────────────────────────

def test_lo_asignado_desde_la_rejilla_tambien_es_override(auth_client, db, manana, ana):
    """Regresion: nacia sin la marca y la regeneracion del mes lo borraba."""
    res = auth_client.post('/cuadrantes/assign', json={
        'cleaner_id': ana.id, 'date': LUNES.isoformat(), 'shift_type_id': manana.id})

    assert res.status_code == 200
    assert ShiftAssignment.query.one().is_override is True


def test_la_rejilla_mensual_sigue_abriendo(auth_client, db, manana):
    assert auth_client.get('/cuadrantes').status_code == 200


# ── El cuadrante de siempre, de una vez ───────────────────────────────────────

def test_crear_el_cuadrante_base_deja_turnos_y_puestos(auth_client, db):
    auth_client.post('/cuadrantes/puestos/crear-base', follow_redirects=True)

    codigos = {p.code for p in ShiftPosition.query.all()}
    assert {'M1', 'M2', 'M3', 'CRM', 'RFM'} <= codigos
    assert {'T1', 'T2', 'T3', 'CRT', 'RFT'} <= codigos
    assert {'NIT A', 'NIT B', 'NIT C'} <= codigos
    assert {'COCINA', 'RECEP'} <= codigos


def test_el_cuadrante_base_deja_los_reflejos_puestos(auth_client, db):
    auth_client.post('/cuadrantes/puestos/crear-base', follow_redirects=True)

    nit_c = ShiftPosition.query.filter_by(code='NIT C').one()
    nit_a = ShiftPosition.query.filter_by(code='NIT A').one()
    assert (nit_c.echo_row, nit_c.echo_previous_day) == ('manana', True)
    assert (nit_a.echo_row, nit_a.echo_previous_day) == ('tarde', False)


def test_el_cuadrante_base_coloca_cada_turno_en_su_fila(auth_client, db):
    auth_client.post('/cuadrantes/puestos/crear-base', follow_redirects=True)

    filas = {st.name: st.board_row for st in ShiftType.query.all()}
    assert filas['Mañana'] == 'manana'
    assert filas['Tarde'] == 'tarde'
    assert filas['Noche C'] == 'noche'
    assert filas['Cocina'] == 'lateral'


def test_pulsarlo_dos_veces_no_duplica_nada(auth_client, db):
    auth_client.post('/cuadrantes/puestos/crear-base', follow_redirects=True)
    cuantos = ShiftPosition.query.count()

    res = auth_client.post('/cuadrantes/puestos/crear-base', follow_redirects=True)

    assert ShiftPosition.query.count() == cuantos
    assert 'no se ha anadido nada' in res.get_data(as_text=True)


def test_no_pisa_el_horario_de_un_turno_que_ya_existe(auth_client, db, manana):
    """Las horas las pone quien las sabe; esto solo monta la forma del recuadro.

    La fixture se llama «Manana» sin tilde, que es justo el caso que no puede
    acabar con un «Mañana» duplicado al lado.
    """
    manana.start_time = time(8, 0)
    manana.board_row = None
    db.session.commit()

    auth_client.post('/cuadrantes/puestos/crear-base', follow_redirects=True)

    db.session.expire_all()
    st = ShiftType.query.filter_by(name='Manana').one()
    assert st.start_time == time(8, 0)      # intacto
    assert st.board_row == 'manana'         # lo que faltaba, si
    assert ShiftType.query.filter_by(name='Mañana').first() is None


def test_crear_el_cuadrante_base_sin_admin_no_puede(client, db, cleaner_user):
    client.post('/admin/login',
                data={'username': 'limpiadora1', 'password': 'limpia123'},
                follow_redirects=True)
    res = client.post('/cuadrantes/puestos/crear-base')
    assert res.status_code in (302, 403)
    assert ShiftPosition.query.count() == 0


# ── Anadir un puesto desde el propio recuadro ─────────────────────────────────

def test_anadir_un_m4_desde_el_tablero(auth_client, db, manana):
    auth_client.post(f'/cuadrantes/tablero/dia/{LUNES}/puesto', data={
        'shift_type_id': manana.id, 'code': 'm4', 'name': 'Cuarta de manana'},
        follow_redirects=True)

    p = ShiftPosition.query.filter_by(code='M4').one()
    assert p.shift_type_id == manana.id
    assert p.name == 'Cuarta de manana'


def test_el_puesto_nuevo_va_al_final_de_su_turno(auth_client, db, manana):
    _puesto(db, manana, 'M1', 0)
    _puesto(db, manana, 'M2', 1)

    auth_client.post(f'/cuadrantes/tablero/dia/{LUNES}/puesto', data={
        'shift_type_id': manana.id, 'code': 'M3'}, follow_redirects=True)

    assert ShiftPosition.query.filter_by(code='M3').one().sort_order == 2


def test_se_puede_crear_el_turno_de_paso(auth_client, db):
    """Una recepcion de tarde no tiene las horas de la de manana."""
    auth_client.post(f'/cuadrantes/tablero/dia/{LUNES}/puesto', data={
        'shift_type_id': 'nuevo', 'nuevo_nombre': 'Recepcion de tarde',
        'nuevo_desde': '16:00', 'nuevo_hasta': '20:00',
        'nuevo_board_row': 'lateral', 'nuevo_color': '#c4e7f0',
        'code': 'RECEP T'}, follow_redirects=True)

    st = ShiftType.query.filter_by(name='Recepcion de tarde').one()
    assert st.board_row == 'lateral'
    assert st.start_time == time(16, 0)
    assert ShiftPosition.query.filter_by(code='RECEP T').one().shift_type_id == st.id


def test_el_turno_nuevo_sin_horas_no_se_crea(auth_client, db):
    res = auth_client.post(f'/cuadrantes/tablero/dia/{LUNES}/puesto', data={
        'shift_type_id': 'nuevo', 'nuevo_nombre': 'Lo que sea',
        'nuevo_board_row': 'tarde', 'code': 'XX'}, follow_redirects=True)

    assert 'falta el nombre o las horas' in res.get_data(as_text=True)
    assert ShiftPosition.query.count() == 0


def test_no_se_repite_el_codigo_desde_el_tablero(auth_client, db, manana):
    _puesto(db, manana, 'M1')

    res = auth_client.post(f'/cuadrantes/tablero/dia/{LUNES}/puesto', data={
        'shift_type_id': manana.id, 'code': 'M1'}, follow_redirects=True)

    assert 'Ya hay un puesto M1' in res.get_data(as_text=True)
    assert ShiftPosition.query.count() == 1


def test_anadir_un_puesto_sin_admin_no_puede(client, db, cleaner_user):
    client.post('/admin/login',
                data={'username': 'limpiadora1', 'password': 'limpia123'},
                follow_redirects=True)
    res = client.post(f'/cuadrantes/tablero/dia/{LUNES}/puesto', data={'code': 'M9'})
    assert res.status_code in (302, 403)
    assert ShiftPosition.query.count() == 0


# ── La vista de mes ───────────────────────────────────────────────────────────

def test_el_mes_sin_sesion_redirige_al_login(client, db):
    res = client.get('/cuadrantes/tablero/mes')
    assert res.status_code in (301, 302)
    assert '/admin/login' in res.headers['Location']


def test_el_mes_ensena_todos_sus_dias(auth_client, db, manana):
    _puesto(db, manana, 'M1')

    texto = auth_client.get('/cuadrantes/tablero/mes?mes=2026-10').get_data(as_text=True)

    assert 'Octubre 2026' in texto
    # Octubre de 2026 empieza en jueves y acaba en sabado: el calendario se
    # estira hasta el lunes anterior y el domingo siguiente.
    assert '/cuadrantes/tablero/dia/2026-09-28' in texto   # lunes de la primera
    assert '/cuadrantes/tablero/dia/2026-10-31' in texto
    assert '/cuadrantes/tablero/dia/2026-11-01' in texto   # domingo de la ultima


def test_el_mes_agrupa_en_semanas_completas(auth_client, db, manana):
    _puesto(db, manana, 'M1')

    texto = auth_client.get('/cuadrantes/tablero/mes?mes=2026-10').get_data(as_text=True)

    # Una fila por semana, y cada una con su enlace a la vista de semana.
    assert texto.count('Abrir la semana') == 5


def test_el_mes_dice_el_estado_de_cada_semana(auth_client, db, manana, ana):
    m1 = _puesto(db, manana, 'M1')
    _asignar(auth_client, LUNES, m1, ana)
    auth_client.post('/cuadrantes/tablero/publicar', json={'week_start': LUNES.isoformat()})

    texto = auth_client.get('/cuadrantes/tablero/mes?mes=2026-10').get_data(as_text=True)

    assert 'Publicada' in texto
    assert 'Borrador' in texto          # las demas semanas del mes


def test_un_mes_mal_escrito_cae_en_el_de_hoy(auth_client, db, manana):
    _puesto(db, manana, 'M1')
    assert auth_client.get('/cuadrantes/tablero/mes?mes=pasado').status_code == 200


def test_el_mes_sin_puestos_lo_dice(auth_client, db):
    texto = auth_client.get('/cuadrantes/tablero/mes').get_data(as_text=True)
    assert 'no hay puestos definidos' in texto


# ── El ancho de las columnas ──────────────────────────────────────────────────
#
# El recuadro viene de una hoja de calculo, donde la columna de la coordinadora
# se deja estrecha y la de M1 ancha. Lo que se protege aqui es que el ancho se
# guarde donde todos lo ven, que no se pueda meter un valor absurdo y que un
# ajuste corrupto no se lleve por delante el tablero entero.

URL_ANCHOS = '/cuadrantes/tablero/anchos'


def _anchos_guardados():
    from app.blueprints.shifts import CLAVE_ANCHOS
    return json.loads(AppSetting.get(CLAVE_ANCHOS, '{}'))


def test_el_ancho_se_guarda_y_sale_en_el_recuadro(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1')

    res = auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 148})

    assert res.status_code == 200
    assert _anchos_guardados() == {str(m1.id): 148}

    texto = auth_client.get(f'/cuadrantes/tablero/dia/{LUNES.isoformat()}').get_data(as_text=True)
    assert 'style="width:148px"' in texto


def test_el_ancho_se_recorta_a_lo_que_cabe(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1')

    auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 10})
    assert _anchos_guardados() == {str(m1.id): 70}

    auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 9999})
    assert _anchos_guardados() == {str(m1.id): 420}


def _ancho_pintado(texto, puesto):
    """El `data-ancho` de la columna de ese puesto, o None si no lleva ninguno."""
    trozo = re.search(r'data-clave="%d"(.*?)>' % puesto.id, texto, re.S)
    assert trozo, f'la columna de {puesto.code} no sale en el recuadro'
    px = re.search(r'data-ancho="(\d+)"', trozo.group(1))
    return int(px.group(1)) if px else None


def test_cada_columna_lleva_su_ancho(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1', orden=1)
    m2 = _puesto(db, manana, 'M2', orden=2)

    auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 200})

    texto = auth_client.get(f'/cuadrantes/tablero/dia/{LUNES.isoformat()}').get_data(as_text=True)
    assert _ancho_pintado(texto, m1) == 200
    # La que nadie ha tocado sigue repartiendose el sitio.
    assert _ancho_pintado(texto, m2) is None
    assert texto.count('data-ancho=') == 1


def test_quitar_el_ancho_devuelve_la_columna_a_automatica(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1', orden=1)
    m2 = _puesto(db, manana, 'M2', orden=2)
    auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 148})
    auth_client.post(URL_ANCHOS, json={'position_id': m2.id, 'width': 90})

    res = auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': None})

    assert res.status_code == 200
    # Solo esa: el doble clic en un tirador no toca las demas columnas.
    assert _anchos_guardados() == {str(m2.id): 90}


def test_restablecer_las_devuelve_todas(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1', orden=1)
    m2 = _puesto(db, manana, 'M2', orden=2)
    auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 148})
    auth_client.post(URL_ANCHOS, json={'position_id': m2.id, 'width': 90})

    res = auth_client.post(URL_ANCHOS, json={'reset': True})

    assert res.status_code == 200
    assert res.get_json()['anchos'] == {}
    assert _anchos_guardados() == {}


def test_restablecer_sin_nada_puesto_no_falla(auth_client, db, manana):
    _puesto(db, manana, 'M1')
    assert auth_client.post(URL_ANCHOS, json={'reset': True}).status_code == 200


def test_un_puesto_que_no_existe_no_guarda_nada(auth_client, db, manana):
    _puesto(db, manana, 'M1')

    res = auth_client.post(URL_ANCHOS, json={'position_id': 9999, 'width': 148})

    assert res.status_code == 404
    assert _anchos_guardados() == {}


def test_un_puesto_de_baja_tampoco(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1')
    m1.active = False
    db.session.commit()

    assert auth_client.post(URL_ANCHOS,
                            json={'position_id': m1.id, 'width': 148}).status_code == 404


def test_sin_puesto_ni_reset_es_peticion_invalida(auth_client, db, manana):
    _puesto(db, manana, 'M1')
    assert auth_client.post(URL_ANCHOS, json={}).status_code == 400
    assert auth_client.post(URL_ANCHOS, json={'width': 148}).status_code == 400


def test_un_ancho_que_no_es_numero_se_rechaza(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1')
    res = auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 'ancho'})
    assert res.status_code == 400
    assert _anchos_guardados() == {}


def test_el_ajuste_queda_en_la_auditoria(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1')
    auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 148})

    registro = AuditLog.query.filter_by(table_name='app_setting').first()
    assert registro is not None
    assert 'M1' in (registro.details or '')


@pytest.mark.parametrize('basura', ['no es json', '[]', '{"a": "b"}', '', 'null'])
def test_un_ajuste_corrupto_no_tumba_el_tablero(auth_client, db, manana, basura):
    """Un AppSetting ilegible deja las columnas como siempre, no una pantalla de error."""
    from app.blueprints.shifts import CLAVE_ANCHOS, _anchos_columnas

    _puesto(db, manana, 'M1')
    db.session.add(AppSetting(key=CLAVE_ANCHOS, value=basura))
    db.session.commit()

    assert _anchos_columnas() == {}
    res = auth_client.get(f'/cuadrantes/tablero/dia/{LUNES.isoformat()}')
    assert res.status_code == 200
    assert 'data-ancho=' not in res.get_data(as_text=True)


def test_un_puesto_borrado_deja_su_ancho_huerfano_sin_molestar(auth_client, db, manana):
    """El ancho de un puesto que ya no esta se ignora: esa columna no existe."""
    m1 = _puesto(db, manana, 'M1', orden=1)
    m2 = _puesto(db, manana, 'M2', orden=2)
    auth_client.post(URL_ANCHOS, json={'position_id': m1.id, 'width': 148})
    db.session.delete(m1)
    db.session.commit()

    texto = auth_client.get(f'/cuadrantes/tablero/dia/{LUNES.isoformat()}').get_data(as_text=True)
    assert 'data-ancho=' not in texto
    assert f'data-clave="{m2.id}"' in texto


# ── El orden de las columnas ──────────────────────────────────────────────────
#
# El reparto de serie pone los puestos de fuera a los dos lados, pero cada
# residencia dibuja su cuadrante como lo tiene en la cabeza. Lo que se protege
# aqui es que lo colocado se respete, que un puesto nuevo no desaparezca y que
# uno borrado no deje hueco.

URL_ORDEN = '/cuadrantes/tablero/orden'


_SIN_ICONOS = re.compile(r'<i [^>]*></i>')


def _columnas_del_recuadro(texto):
    """[[codigo, ...], ...]: las columnas del recuadro y lo que lleva cada una."""
    cuerpo = texto.split('<div class="tb-lienzo">')[1]
    for corte in ('class="tb-noche"', 'id="tb-banquillo"'):
        if corte in cuerpo:
            cuerpo = cuerpo.split(corte)[0]
            break
    columnas = []
    for trozo in cuerpo.split('<div class="tb-col"')[1:]:
        columnas.append([_SIN_ICONOS.sub('', c.split('</div>')[0]).strip()
                         for c in trozo.split('<div class="tb-code">')[1:]])
    return columnas


def _codigos_de_columna(texto):
    """El codigo de arriba de cada columna, de izquierda a derecha."""
    return [col[0] for col in _columnas_del_recuadro(texto) if col]


def _recuadro(client, dia=LUNES):
    return client.get(f'/cuadrantes/tablero/dia/{dia.isoformat()}').get_data(as_text=True)


def test_sin_orden_guardado_manda_el_reparto_de_siempre(auth_client, db, manana, cocina):
    """Los puestos de fuera a los lados y los de planta en medio."""
    _puesto(db, manana, 'M1', orden=1)
    _puesto(db, cocina, 'Cocina', orden=1)

    assert _codigos_de_columna(_recuadro(auth_client)) == ['Cocina', 'M1']


def test_una_columna_se_puede_llevar_a_la_derecha(auth_client, db, manana, cocina):
    m1 = _puesto(db, manana, 'M1', orden=1)
    co = _puesto(db, cocina, 'Cocina', orden=1)

    res = auth_client.post(URL_ORDEN, json={'orden': [m1.id, co.id]})

    assert res.status_code == 200
    assert _codigos_de_columna(_recuadro(auth_client)) == ['M1', 'Cocina']


def test_el_orden_tambien_manda_en_la_semana(auth_client, db, manana, cocina):
    """La semana son los mismos recuadros en pequeno: no pueden salir al reves."""
    m1 = _puesto(db, manana, 'M1', orden=1)
    co = _puesto(db, cocina, 'Cocina', orden=1)
    auth_client.post(URL_ORDEN, json={'orden': [m1.id, co.id]})

    texto = auth_client.get(f'/cuadrantes/tablero?semana={LUNES.isoformat()}').get_data(as_text=True)

    # En las miniaturas el codigo va en su propia etiqueta, no en una caja.
    assert texto.index('M1') < texto.index('Cocina')


def test_un_puesto_nuevo_sale_al_final(auth_client, db, manana, cocina):
    """Hasta que se coloque: al final se ve que hay algo por situar."""
    m1 = _puesto(db, manana, 'M1', orden=1)
    co = _puesto(db, cocina, 'Cocina', orden=1)
    auth_client.post(URL_ORDEN, json={'orden': [m1.id, co.id]})

    _puesto(db, manana, 'M2', orden=2)

    assert _codigos_de_columna(_recuadro(auth_client)) == ['M1', 'Cocina', 'M2']


def test_un_puesto_borrado_no_deja_hueco(auth_client, db, manana, cocina):
    m1 = _puesto(db, manana, 'M1', orden=1)
    m2 = _puesto(db, manana, 'M2', orden=2)
    co = _puesto(db, cocina, 'Cocina', orden=1)
    auth_client.post(URL_ORDEN, json={'orden': [m2.id, co.id, m1.id]})

    db.session.delete(co)
    db.session.commit()

    assert _codigos_de_columna(_recuadro(auth_client)) == ['M2', 'M1']


def test_restablecer_devuelve_el_reparto_de_siempre(auth_client, db, manana, cocina):
    m1 = _puesto(db, manana, 'M1', orden=1)
    co = _puesto(db, cocina, 'Cocina', orden=1)
    auth_client.post(URL_ORDEN, json={'orden': [m1.id, co.id]})

    res = auth_client.post(URL_ORDEN, json={'reset': True})

    assert res.status_code == 200
    assert res.get_json()['orden'] == []
    assert _codigos_de_columna(_recuadro(auth_client)) == ['Cocina', 'M1']


def test_un_orden_repetido_se_queda_con_la_primera_vez(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1', orden=1)
    m2 = _puesto(db, manana, 'M2', orden=2)

    res = auth_client.post(URL_ORDEN, json={'orden': [m2.id, m1.id, m2.id]})

    # Un puesto esta en una columna y en una sola.
    assert res.get_json()['orden'] == [[m2.id], [m1.id]]


@pytest.mark.parametrize('cuerpo', [
    {}, {'orden': []}, {'orden': 'M1,M2'}, {'orden': ['dos']}, {'orden': list(range(200))},
])
def test_un_orden_invalido_se_rechaza(auth_client, db, manana, cuerpo):
    _puesto(db, manana, 'M1')
    assert auth_client.post(URL_ORDEN, json=cuerpo).status_code == 400


@pytest.mark.parametrize('basura', ['no es json', '{}', '', 'null', '[{"a": 1}]'])
def test_un_orden_corrupto_no_tumba_el_tablero(auth_client, db, manana, cocina, basura):
    from app.blueprints.shifts import CLAVE_ORDEN, _orden_columnas

    _puesto(db, manana, 'M1', orden=1)
    _puesto(db, cocina, 'Cocina', orden=1)
    db.session.add(AppSetting(key=CLAVE_ORDEN, value=basura))
    db.session.commit()

    assert _orden_columnas() == []
    assert _codigos_de_columna(_recuadro(auth_client)) == ['Cocina', 'M1']


def test_el_orden_queda_en_la_auditoria(auth_client, db, manana):
    m1 = _puesto(db, manana, 'M1', orden=1)
    m2 = _puesto(db, manana, 'M2', orden=2)
    auth_client.post(URL_ORDEN, json={'orden': [m2.id, m1.id]})

    detalles = [r.details or '' for r in
                AuditLog.query.filter_by(table_name='app_setting').all()]
    assert any('shift_board_col_order' in d for d in detalles)


# ── Apilar dos cuadros en la misma columna ────────────────────────────────────
#
# Una columna es una franja de tiempo leida de arriba abajo, asi que apilar solo
# vale si los dos turnos no coinciden. Si coincidieran, el recuadro estaria
# mintiendo sobre el horario, que es justo para lo que se mira.

def test_dos_puestos_que_no_coinciden_caben_en_una_columna(auth_client, db, manana, tarde):
    m1 = _puesto(db, manana, 'M1', orden=1)
    t1 = _puesto(db, tarde, 'T1', orden=1)

    res = auth_client.post(URL_ORDEN, json={'orden': [[m1.id, t1.id]]})

    assert res.status_code == 200
    assert _columnas_del_recuadro(_recuadro(auth_client)) == [['M1', 'T1']]


def test_apilar_dos_que_coinciden_se_rechaza(auth_client, db, cocina, recepcion):
    co = _puesto(db, cocina, 'Cocina', orden=1)
    re_ = _puesto(db, recepcion, 'Recep', orden=1)

    res = auth_client.post(URL_ORDEN, json={'orden': [[co.id, re_.id]]})

    assert res.status_code == 400
    assert 'Cocina' in res.get_json()['error'] and 'Recep' in res.get_json()['error']
    # Y no se guarda nada: siguen una al lado de la otra.
    assert _columnas_del_recuadro(_recuadro(auth_client)) == [['Cocina'], ['Recep']]


def test_un_cuadro_apilado_vuelve_a_columna_propia(auth_client, db, manana, tarde):
    m1 = _puesto(db, manana, 'M1', orden=1)
    t1 = _puesto(db, tarde, 'T1', orden=1)
    auth_client.post(URL_ORDEN, json={'orden': [[m1.id, t1.id]]})

    auth_client.post(URL_ORDEN, json={'orden': [[t1.id], [m1.id]]})

    assert _columnas_del_recuadro(_recuadro(auth_client)) == [['T1'], ['M1']]


def test_el_formato_antiguo_de_lista_plana_se_entiende(auth_client, db, manana, cocina):
    """Lo guardado antes de que una columna pudiera llevar dos puestos."""
    from app.blueprints.shifts import CLAVE_ORDEN

    m1 = _puesto(db, manana, 'M1', orden=1)
    co = _puesto(db, cocina, 'Cocina', orden=1)
    db.session.add(AppSetting(key=CLAVE_ORDEN, value=json.dumps([m1.id, co.id])))
    db.session.commit()

    assert _columnas_del_recuadro(_recuadro(auth_client)) == [['M1'], ['Cocina']]


def test_un_puesto_no_puede_estar_en_dos_columnas(auth_client, db, manana, tarde):
    m1 = _puesto(db, manana, 'M1', orden=1)
    t1 = _puesto(db, tarde, 'T1', orden=1)

    res = auth_client.post(URL_ORDEN, json={'orden': [[m1.id, t1.id], [m1.id]]})

    # La segunda vez sobra; la columna que queda vacia no se guarda.
    assert res.get_json()['orden'] == [[m1.id, t1.id]]
