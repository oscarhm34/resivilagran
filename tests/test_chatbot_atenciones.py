"""
test_chatbot_atenciones.py — El chatbot y los tipos de atención con subtipos.

Contexto: "Deposiciones" tiene tres hijos (FECALOMA, NORMAL, DIARREA) y en el
registro solo se guarda el hijo. El chatbot devolvía "DIARREA" a secas, sin decir
de qué categoría era, y no tenía ninguna herramienta para buscar por tipo. A la
pregunta "quién ha hecho deposiciones" no sabía contestar.

Lo que se fija aquí:
  - que el nombre del tipo llega con su categoría delante;
  - que se puede buscar por categoría y por subtipo, con y sin tildes;
  - que el periodo de varios días funciona;
  - y que un término que no existe devuelve los tipos disponibles, no un vacío
    mudo que el modelo interpretaría como "no ha pasado nada".
"""

import json
from datetime import datetime, timedelta

import pytest

from app.chatbot import (_atenciones_por_tipo, _atenciones_hoy, _nombre_tipo,
                         _tipos_de, _tipos_de_atencion, _tipos_que_coinciden)
from app.models import CareRecord, CareType, Resident


@pytest.fixture
def deposiciones(db):
    """La categoría real que motivó el arreglo, con sus tres subtipos."""
    padre = CareType(name='Deposiciones', active=True)
    db.session.add(padre)
    db.session.flush()
    hijos = {}
    for nombre in ('FECALOMA', 'NORMAL', 'DIARREA'):
        h = CareType(name=nombre, parent_id=padre.id, active=True)
        db.session.add(h)
        hijos[nombre] = h
    db.session.commit()
    return padre, hijos


@pytest.fixture
def residente(db):
    r = Resident(name='Josefa Ruiz', nfc_code='RES-CHAT-1',
                 room_number='12', active=True)
    db.session.add(r)
    db.session.commit()
    return r


def _atencion(db, cleaner_user, residente, tipos, cuando=None, notas=None):
    inicio = cuando or datetime.now().replace(hour=10, minute=30)
    rec = CareRecord(worker_id=cleaner_user.id, resident_id=residente.id,
                     start_time=inicio, end_time=inicio + timedelta(minutes=10),
                     notes=notas)
    db.session.add(rec)
    db.session.flush()
    for ct in tipos:
        rec.care_types.append(ct)
    db.session.commit()
    return rec


# ── El nombre lleva la categoría delante ─────────────────────────────────────

def test_el_subtipo_se_nombra_con_su_categoria(db, deposiciones):
    _, hijos = deposiciones

    assert _nombre_tipo(hijos['DIARREA']) == 'Deposiciones: DIARREA'


def test_un_tipo_sin_categoria_se_nombra_a_secas(db):
    suelto = CareType(name='Higiene', active=True)
    db.session.add(suelto)
    db.session.commit()

    assert _nombre_tipo(suelto) == 'Higiene'


def test_un_registro_sin_tipo_no_revienta(db, cleaner_user, residente):
    rec = _atencion(db, cleaner_user, residente, [])

    assert _tipos_de(rec) == 'Sin tipo'


def test_las_atenciones_de_hoy_dicen_de_que_categoria_son(
        db, cleaner_user, residente, deposiciones):
    _, hijos = deposiciones
    _atencion(db, cleaner_user, residente, [hijos['DIARREA']])

    datos = json.loads(_atenciones_hoy())

    assert datos['por_residente']['Josefa Ruiz'][0]['tipo'] == 'Deposiciones: DIARREA'


# ── Buscar por tipo ──────────────────────────────────────────────────────────

def test_buscar_por_categoria_encuentra_todos_los_subtipos(db, deposiciones):
    coincidencias = _tipos_que_coinciden('deposiciones')

    assert {ct.name for ct in coincidencias} == {
        'Deposiciones', 'FECALOMA', 'NORMAL', 'DIARREA'}


def test_buscar_por_subtipo_encuentra_solo_ese(db, deposiciones):
    assert [ct.name for ct in _tipos_que_coinciden('diarrea')] == ['DIARREA']


def test_las_tildes_dan_igual_al_buscar(db):
    ct = CareType(name='Medicación', active=True)
    ct2 = CareType(name='Higiene', active=True)
    db.session.add_all([ct, ct2])
    db.session.commit()

    assert [c.name for c in _tipos_que_coinciden('medicacion')] == ['Medicación']


def test_quien_ha_hecho_deposiciones(db, cleaner_user, residente, deposiciones):
    _, hijos = deposiciones
    _atencion(db, cleaner_user, residente, [hijos['DIARREA']], notas='Blanda')

    datos = json.loads(_atenciones_por_tipo('deposiciones'))

    assert datos['total'] == 1
    assert datos['residentes_distintos'] == 1
    atencion = datos['atenciones'][0]
    assert atencion['residente'] == 'Josefa Ruiz'
    assert atencion['habitacion'] == '12'
    assert atencion['tipo'] == 'Deposiciones: DIARREA'
    assert atencion['notas'] == 'Blanda'


def test_solo_se_listan_los_tipos_por_los_que_se_pregunta(
        db, cleaner_user, residente, deposiciones):
    """Con Higiene y Deposiciones en el mismo registro, la respuesta no mezcla."""
    _, hijos = deposiciones
    higiene = CareType(name='Higiene', active=True)
    db.session.add(higiene)
    db.session.commit()
    _atencion(db, cleaner_user, residente, [hijos['NORMAL'], higiene])

    datos = json.loads(_atenciones_por_tipo('Deposiciones'))

    assert datos['atenciones'][0]['tipo'] == 'Deposiciones: NORMAL'


def test_una_atencion_de_otro_tipo_no_cuenta(
        db, cleaner_user, residente, deposiciones):
    higiene = CareType(name='Higiene', active=True)
    db.session.add(higiene)
    db.session.commit()
    _atencion(db, cleaner_user, residente, [higiene])

    assert json.loads(_atenciones_por_tipo('deposiciones'))['total'] == 0


def test_por_defecto_solo_mira_hoy(db, cleaner_user, residente, deposiciones):
    _, hijos = deposiciones
    ayer = datetime.now().replace(hour=9, minute=0) - timedelta(days=1)
    _atencion(db, cleaner_user, residente, [hijos['NORMAL']], cuando=ayer)

    assert json.loads(_atenciones_por_tipo('deposiciones'))['total'] == 0


def test_con_dias_se_mira_hacia_atras(db, cleaner_user, residente, deposiciones):
    _, hijos = deposiciones
    ayer = datetime.now().replace(hour=9, minute=0) - timedelta(days=1)
    _atencion(db, cleaner_user, residente, [hijos['NORMAL']], cuando=ayer)
    _atencion(db, cleaner_user, residente, [hijos['DIARREA']])

    datos = json.loads(_atenciones_por_tipo('deposiciones', dias=7))

    assert datos['total'] == 2
    assert 'a' in datos['periodo']


def test_un_tipo_que_no_existe_devuelve_los_que_si(db, deposiciones):
    """Un vacío mudo se leeria como 'no ha pasado nada', que es otra cosa."""
    datos = json.loads(_atenciones_por_tipo('radiografias'))

    assert 'error' in datos
    assert 'Deposiciones: DIARREA' in datos['tipos_disponibles']


# ── Catálogo de tipos ────────────────────────────────────────────────────────

def test_el_catalogo_agrupa_los_subtipos_por_categoria(db, deposiciones):
    datos = json.loads(_tipos_de_atencion())

    deposi = next(t for t in datos['tipos'] if t['nombre'] == 'Deposiciones')
    assert set(deposi['subtipos']) == {'FECALOMA', 'NORMAL', 'DIARREA'}


def test_el_catalogo_no_saca_los_subtipos_como_categorias(db, deposiciones):
    datos = json.loads(_tipos_de_atencion())

    assert [t['nombre'] for t in datos['tipos']] == ['Deposiciones']
