"""
test_pintura_personal.py — Que no haya dos personas pintadas igual.

El tablero se lee por el color: en la vista del mes no caben los nombres, asi
que el recuadro naranja *es* Chahrazad. Con quince colores y treinta personas
eso se rompe solo, y dos naranjas iguales convierten la vista del mes en algo
que no se puede descifrar. El segundo eje es el relleno —liso, rayas, puntos,
cuadricula—, que ademas sobrevive a una impresion en blanco y negro y a quien
no distingue el rojo del verde.

Lo que se protege aqui: que la combinacion de cada persona sea estable (se
aprende con el tiempo, no puede bailar cuando entra alguien nuevo), que el
reparto automatico deje a todo el mundo distinto, y que al arreglar un choque
no se le cambie el color a quien no hacia falta.
"""

from datetime import date, time, timedelta

import pytest

from app.models import (
    Cleaner, ShiftAssignment, ShiftPosition, ShiftType,
)


LUNES = date(2026, 10, 5)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _trabajadora(db, nombre, usuario, color=None, patron=None, activa=True):
    c = Cleaner(username=usuario, name=nombre, is_admin=False, active=activa,
                role='atenciones', color=color, pattern=patron)
    c.set_password('x123456')
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture
def manana(db):
    st = ShiftType(name='Manana', short_name='M', color='#8ab4e8',
                   start_time=time(7, 30), end_time=time(14, 0),
                   sort_order=1, active=True, board_row='manana')
    db.session.add(st)
    db.session.commit()
    return st


@pytest.fixture
def m1(db, manana):
    p = ShiftPosition(shift_type_id=manana.id, code='M1', sort_order=0)
    db.session.add(p)
    db.session.commit()
    return p


# ── La combinacion de cada una ────────────────────────────────────────────────

def test_el_relleno_elegido_a_mano_manda(app, db):
    from app.blueprints.shifts import _patron_trabajadora
    c = _trabajadora(db, 'Ana Pons', 'ana', color='#e03131', patron='rayas')
    assert _patron_trabajadora(c) == 'rayas'


def test_un_relleno_inventado_no_se_usa(app, db):
    """Lo que venga mal guardado no puede colarse como clase CSS."""
    from app.blueprints.shifts import PATRONES_PERSONAL, _patron_trabajadora
    c = _trabajadora(db, 'Ana Pons', 'ana', patron='arcoiris')
    assert _patron_trabajadora(c) in PATRONES_PERSONAL


def test_sin_elegir_nada_la_combinacion_es_estable(app, db):
    """Se deriva del id: el color se aprende y no puede bailar."""
    from app.blueprints.shifts import _pinta
    c = _trabajadora(db, 'Ana Pons', 'ana')
    primera = _pinta(c)
    # Entra alguien nuevo, y luego otra persona se va.
    otra = _trabajadora(db, 'Berta Lluch', 'berta')
    db.session.delete(otra)
    db.session.commit()
    assert _pinta(c) == primera


def test_la_paleta_y_los_rellenos_dan_sesenta_combinaciones(app):
    from app.blueprints.shifts import (
        PALETA_PERSONAL, PATRONES_PERSONAL, _pares_de_pintura)
    pares = list(_pares_de_pintura())
    assert len(pares) == len(set(pares))
    assert len(pares) == len(PALETA_PERSONAL) * len(PATRONES_PERSONAL)


def test_los_colores_se_agotan_antes_de_pasar_al_siguiente_relleno(app):
    """Los lisos primero: son los mas faciles de reconocer."""
    from app.blueprints.shifts import PALETA_PERSONAL, _pares_de_pintura
    pares = list(_pares_de_pintura())
    assert {p for _, p in pares[:len(PALETA_PERSONAL)]} == {'liso'}


# ── La tinta del relleno ──────────────────────────────────────────────────────

@pytest.mark.parametrize('color', ['#f2e14c', '#c9ccd1', '#f3a6a6', '#ffffff'])
def test_sobre_un_color_claro_las_rayas_van_oscuras(app, color):
    from app.blueprints.shifts import _tinta_sobre
    assert _tinta_sobre(color).startswith('rgba(0,0,0')


@pytest.mark.parametrize('color', ['#1565c0', '#3f7d3f', '#6a1b9a', '#000000'])
def test_sobre_un_color_oscuro_las_rayas_van_claras(app, color):
    from app.blueprints.shifts import _tinta_sobre
    assert _tinta_sobre(color).startswith('rgba(255,255,255')


def test_un_color_mal_escrito_no_revienta_el_tablero(app):
    from app.blueprints.shifts import _tinta_sobre
    assert _tinta_sobre('no es un color').startswith('rgba(')
    assert _tinta_sobre(None).startswith('rgba(')


# ── Detectar los choques ──────────────────────────────────────────────────────

def test_dos_personas_con_el_mismo_color_y_relleno_son_un_choque(app, db):
    from app.blueprints.shifts import _choques_de_pintura
    a = _trabajadora(db, 'Ana Pons', 'ana', color='#f0a01e', patron='liso')
    b = _trabajadora(db, 'Berta Lluch', 'berta', color='#f0a01e', patron='liso')
    _trabajadora(db, 'Carmen Roig', 'carmen', color='#f0a01e', patron='rayas')

    choques = _choques_de_pintura()
    assert len(choques) == 1
    assert {c.id for c in choques[0]} == {a.id, b.id}


def test_el_mismo_color_con_distinto_relleno_no_choca(app, db):
    from app.blueprints.shifts import _choques_de_pintura
    _trabajadora(db, 'Ana Pons', 'ana', color='#f0a01e', patron='liso')
    _trabajadora(db, 'Berta Lluch', 'berta', color='#f0a01e', patron='puntos')
    assert _choques_de_pintura() == []


def test_quien_esta_de_baja_no_cuenta_como_choque(app, db):
    """Si no sale en el tablero, no estorba a nadie."""
    from app.blueprints.shifts import _choques_de_pintura
    _trabajadora(db, 'Ana Pons', 'ana', color='#f0a01e', patron='liso')
    _trabajadora(db, 'Vieja Cuenta', 'vieja', color='#f0a01e', patron='liso',
                 activa=False)
    assert _choques_de_pintura() == []


# ── Repartir ──────────────────────────────────────────────────────────────────

def test_repartir_deja_a_todo_el_mundo_distinto(app, db):
    from app.blueprints.shifts import _choques_de_pintura, _repartir_pintura
    for i in range(6):
        _trabajadora(db, f'Persona {i}', f'p{i}', color='#f0a01e', patron='liso')

    cambiadas = _repartir_pintura()
    db.session.commit()

    assert len(cambiadas) == 5          # la primera se queda como estaba
    assert _choques_de_pintura() == []


def test_al_repartir_la_primera_de_cada_choque_no_se_toca(app, db):
    """El color se aprende con el tiempo: no se le cambia a quien no hace falta."""
    from app.blueprints.shifts import _pinta, _repartir_pintura
    a = _trabajadora(db, 'Ana Pons', 'ana', color='#f0a01e', patron='liso')
    b = _trabajadora(db, 'Berta Lluch', 'berta', color='#f0a01e', patron='liso')
    antes = _pinta(a)

    _repartir_pintura()
    db.session.commit()

    assert _pinta(a) == antes
    assert _pinta(b) != antes


def test_repartir_no_toca_nada_si_no_hay_choques(app, db):
    from app.blueprints.shifts import _repartir_pintura
    _trabajadora(db, 'Ana Pons', 'ana', color='#f0a01e', patron='liso')
    _trabajadora(db, 'Berta Lluch', 'berta', color='#1565c0', patron='rayas')
    assert _repartir_pintura() == []


def test_repartir_desde_el_panel(auth_client, db):
    from app.blueprints.shifts import _choques_de_pintura
    _trabajadora(db, 'Ana Pons', 'ana', color='#f0a01e', patron='liso')
    _trabajadora(db, 'Berta Lluch', 'berta', color='#f0a01e', patron='liso')

    res = auth_client.post('/cleaners/repartir-pintura', follow_redirects=True)
    assert res.status_code == 200
    assert 'Colores repartidos' in res.get_data(as_text=True)
    assert _choques_de_pintura() == []


def test_repartir_sin_sesion_no_se_puede(client, db):
    _trabajadora(db, 'Ana Pons', 'ana', color='#f0a01e', patron='liso')
    _trabajadora(db, 'Berta Lluch', 'berta', color='#f0a01e', patron='liso')

    res = client.post('/cleaners/repartir-pintura')
    assert res.status_code in (301, 302, 401, 403)
    assert db.session.get(Cleaner, 2).color == '#f0a01e'


# ── Donde tiene que verse ─────────────────────────────────────────────────────

def test_el_relleno_llega_al_recuadro_del_dia(auth_client, db, m1):
    ana = _trabajadora(db, 'Ana Pons', 'ana', color='#1565c0', patron='rayas')
    db.session.add(ShiftAssignment(date=LUNES, cleaner_id=ana.id,
                                   shift_type_id=m1.shift_type_id,
                                   position_id=m1.id, is_override=True))
    db.session.commit()

    texto = auth_client.get(
        f'/cuadrantes/tablero/dia/{LUNES}').get_data(as_text=True)
    assert 'pat-rayas' in texto
    assert '--pat-tinta' in texto


def test_el_relleno_llega_a_la_miniatura_de_la_semana(auth_client, db, m1):
    ana = _trabajadora(db, 'Ana Pons', 'ana', color='#1565c0', patron='puntos')
    db.session.add(ShiftAssignment(date=LUNES, cleaner_id=ana.id,
                                   shift_type_id=m1.shift_type_id,
                                   position_id=m1.id, is_override=True))
    db.session.commit()

    texto = auth_client.get(
        f'/cuadrantes/tablero?semana={LUNES}').get_data(as_text=True)
    assert 'pat-puntos' in texto


def test_el_relleno_llega_al_mes(auth_client, db, m1):
    ana = _trabajadora(db, 'Ana Pons', 'ana', color='#1565c0', patron='malla')
    db.session.add(ShiftAssignment(date=LUNES, cleaner_id=ana.id,
                                   shift_type_id=m1.shift_type_id,
                                   position_id=m1.id, is_override=True))
    db.session.commit()

    texto = auth_client.get(
        '/cuadrantes/tablero/mes?mes=2026-10').get_data(as_text=True)
    assert 'pat-malla' in texto


def test_la_ficha_del_empleado_guarda_el_relleno(auth_client, db):
    ana = _trabajadora(db, 'Ana Pons', 'ana')
    auth_client.post('/cleaners/add_edit', data={
        'cleaner_id': ana.id, 'username': 'ana', 'name': 'Ana Pons',
        'role': 'atenciones', 'active': 'on',
        'color': '#e03131', 'pattern': 'puntos',
    }, follow_redirects=True)

    db.session.expire_all()
    actualizada = db.session.get(Cleaner, ana.id)
    assert actualizada.color == '#e03131'
    assert actualizada.pattern == 'puntos'


def test_la_ficha_rechaza_un_relleno_que_no_existe(auth_client, db):
    ana = _trabajadora(db, 'Ana Pons', 'ana')
    auth_client.post('/cleaners/add_edit', data={
        'cleaner_id': ana.id, 'username': 'ana', 'name': 'Ana Pons',
        'role': 'atenciones', 'active': 'on', 'pattern': '"><script>',
    }, follow_redirects=True)

    db.session.expire_all()
    assert db.session.get(Cleaner, ana.id).pattern is None


def test_el_panel_avisa_de_quien_se_pinta_igual(auth_client, db):
    _trabajadora(db, 'Ana Pons', 'ana', color='#f0a01e', patron='liso')
    _trabajadora(db, 'Berta Lluch', 'berta', color='#f0a01e', patron='liso')

    texto = auth_client.get('/manage_workers').get_data(as_text=True)
    assert 'se pintan igual' in texto
    assert 'Ana Pons, Berta Lluch' in texto
