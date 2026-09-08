"""
test_chatbot_residentes.py — Encontrar a un residente sin saberse el nombre exacto.

Contexto: el chatbot buscaba con un unico `ilike('%texto%')`, que exige que lo
escrito aparezca tal cual, seguido y en ese orden. "Tadea Cazorla" no encontraba a
"CAZORLA CARRILLO TADEA" y el asistente contestaba que ese residente no existe;
solo funcionaba copiando el nombre entero tal como esta en la ficha, que es justo
lo que nadie se sabe de memoria. Los nombres, ademas, viven en un unico campo
libre y con dos formatos mezclados —"NOMBRE APELLIDOS" y "APELLIDOS, NOMBRE"—,
asi que no hay un orden que acertar.

Y el fallo era total, no parcial: las demas herramientas reciben ya un
`residente_id`, de modo que si `buscar_residente` falla no queda alternativa.

Lo que se fija aqui:
  - que el orden de nombre y apellidos da igual, y que basta con una parte;
  - que las tildes no estorban en ninguno de los dos sentidos;
  - que una errata leve sigue encontrando a la persona;
  - que cuando de verdad no hay nada se ofrecen nombres parecidos, en vez de un
    "no existe" seco que cierra la conversacion;
  - que a quien esta de baja se le nombra pero nunca con su `id`, para que el
    modelo no pueda pedir sus datos medicos.
"""

import json

import pytest

from app.chatbot import MAXIMO_RESULTADOS, _buscar_residente, _buscar_trabajador
from app.models import Cleaner, Resident


NOMBRES = [
    'CAZORLA CARRILLO TADEA',       # el caso que motivo el arreglo
    'MANUEL CAZORLA GARCIA',        # mismo apellido, orden contrario
    'ASSUMPCIÓ ZAPATER CARRASQUER',  # con tilde
    'ARMADA PUJOL, TERESA',         # el formato con coma
]


@pytest.fixture
def residentes(db):
    """Los formatos de nombre que conviven de verdad en la residencia."""
    creados = {}
    for i, nombre in enumerate(NOMBRES):
        r = Resident(name=nombre, nfc_code=f'RES-BUSCA-{i}', active=True)
        db.session.add(r)
        creados[nombre] = r
    db.session.commit()
    return creados


def _busca(termino):
    return json.loads(_buscar_residente(termino))


def _nombres(datos):
    return [r['nombre'] for r in datos.get('residentes', [])]


# ── Ni el orden, ni las tildes, ni una errata deben estorbar ─────────────────

def test_el_nombre_de_pila_y_un_apellido_bastan(db, residentes):
    """La pregunta que fallaba: 'Tadea Cazorla' contra 'CAZORLA CARRILLO TADEA'."""
    datos = _busca('Tadea Cazorla')

    assert _nombres(datos) == ['CAZORLA CARRILLO TADEA']


def test_el_orden_de_las_palabras_da_igual(db, residentes):
    assert _nombres(_busca('cazorla tadea')) == ['CAZORLA CARRILLO TADEA']


def test_solo_el_nombre_de_pila_ya_encuentra(db, residentes):
    assert _nombres(_busca('Tadea')) == ['CAZORLA CARRILLO TADEA']


def test_las_tildes_dan_igual_si_no_se_escriben(db, residentes):
    assert _nombres(_busca('Assumpcio Zapater')) == ['ASSUMPCIÓ ZAPATER CARRASQUER']


def test_las_tildes_dan_igual_si_se_escriben(db, residentes):
    assert _nombres(_busca('assumpció')) == ['ASSUMPCIÓ ZAPATER CARRASQUER']


def test_el_formato_con_coma_tambien_se_encuentra(db, residentes):
    """La coma de 'ARMADA PUJOL, TERESA' no puede quedarse pegada al apellido."""
    assert _nombres(_busca('teresa armada')) == ['ARMADA PUJOL, TERESA']


def test_una_errata_leve_no_impide_encontrar(db, residentes):
    assert _nombres(_busca('Cazola Tadea')) == ['CAZORLA CARRILLO TADEA']


def test_un_apellido_compartido_devuelve_a_los_dos(db, residentes):
    assert _nombres(_busca('Cazorla')) == [
        'CAZORLA CARRILLO TADEA', 'MANUEL CAZORLA GARCIA']


def test_devuelve_el_id_con_el_que_seguir_consultando(db, residentes):
    """Sin `id` no se puede pedir ni la ficha ni las atenciones."""
    datos = _busca('Tadea Cazorla')

    assert datos['residentes'][0]['id'] == residentes['CAZORLA CARRILLO TADEA'].id


# ── Cuando no hay coincidencia ───────────────────────────────────────────────

def test_un_nombre_parecido_ofrece_a_quien_se_parece(db, residentes):
    """Un 'no existe' seco cierra la conversacion; una sugerencia la continua."""
    datos = _busca('Cazorro')

    assert 'residentes' not in datos
    assert any('CAZORLA' in n for n in datos['sugerencias'])


def test_un_disparate_no_inventa_sugerencias(db, residentes):
    datos = _busca('xkjqwv')

    assert 'residentes' not in datos
    assert 'sugerencias' not in datos


def test_una_busqueda_vacia_no_devuelve_a_todo_el_mundo(db, residentes):
    datos = _busca('   ')

    assert 'residentes' not in datos


# ── Residentes dados de baja ─────────────────────────────────────────────────

def test_un_residente_de_baja_no_sale_como_coincidencia(db, residentes):
    db.session.add(Resident(name='FERNANDEZ SOLE, JOAQUIM',
                            nfc_code='RES-BAJA-1', active=False))
    db.session.commit()

    assert 'residentes' not in _busca('Joaquim Fernandez')


def test_de_un_residente_de_baja_se_da_el_nombre_pero_nunca_el_id(db, residentes):
    """Sin `id` el modelo no puede pedir su ficha ni sus datos medicos."""
    db.session.add(Resident(name='FERNANDEZ SOLE, JOAQUIM',
                            nfc_code='RES-BAJA-2', active=False))
    db.session.commit()

    datos = _busca('Joaquim Fernandez')

    assert datos['inactivos'] == [
        'FERNANDEZ SOLE, JOAQUIM (consta como dado de baja)']


# ── Demasiadas coincidencias ─────────────────────────────────────────────────

def test_si_hay_mas_de_diez_se_avisa_de_que_la_lista_esta_recortada(db):
    """Recortar en silencio se lee como 'solo hay estos', que es otra cosa."""
    for i in range(MAXIMO_RESULTADOS + 2):
        db.session.add(Resident(name=f'GARCIA PEREZ NOMBRE{i}',
                                nfc_code=f'RES-MUCHOS-{i}', active=True))
    db.session.commit()

    datos = _busca('Garcia')

    assert len(datos['residentes']) == MAXIMO_RESULTADOS
    assert 'mas_resultados' in datos


# ── Las trabajadoras se buscan igual ─────────────────────────────────────────

def _crea_trabajadora(db):
    trabajadora = Cleaner(username='mgarcia', name='GARCIA LOPEZ, MARIA',
                          is_admin=False, active=True)
    trabajadora.set_password('secreta123')
    db.session.add(trabajadora)
    db.session.commit()
    return trabajadora


def test_las_trabajadoras_se_buscan_con_el_mismo_criterio(db):
    _crea_trabajadora(db)

    datos = json.loads(_buscar_trabajador('maria garcia'))

    assert [t['nombre'] for t in datos['trabajadores']] == ['GARCIA LOPEZ, MARIA']


def test_una_errata_tampoco_impide_encontrar_a_una_trabajadora(db):
    _crea_trabajadora(db)

    datos = json.loads(_buscar_trabajador('Garcio Maria'))

    assert [t['nombre'] for t in datos['trabajadores']] == ['GARCIA LOPEZ, MARIA']


def test_una_trabajadora_que_no_existe_ofrece_las_parecidas(db):
    _crea_trabajadora(db)

    datos = json.loads(_buscar_trabajador('Lopera'))

    assert 'trabajadores' not in datos
    assert 'GARCIA LOPEZ, MARIA' in datos['sugerencias']
