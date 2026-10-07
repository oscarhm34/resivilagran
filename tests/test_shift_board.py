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

from datetime import date, datetime, time, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import (
    Absence, AbsenceType, AuditLog, Cleaner, Notification, ShiftAssignment,
    ShiftPosition, ShiftType, ShiftWeekPublication,
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
