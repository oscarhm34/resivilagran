"""
test_color_pertenencias.py — Deduccion del color dominante de una foto.

Lo que se protege aqui: que el color que se ensena en el inventario y por el que
se filtra sea el que una persona diria, sobre todo en los casos que el
histograma de identificacion no puede resolver porque tira el brillo: marron
contra naranja, y blanco contra negro.

Es logica de negocio pura: se prueba la funcion directamente, sin HTTP.
"""

import pytest
from PIL import Image, ImageDraw

from app.image_match import BELONGING_COLORS, color_dominante


def _plano(rgb, tam=(200, 200)) -> Image.Image:
    return Image.new('RGB', tam, rgb)


# ── Colores planos ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('rgb, esperado', [
    ((220, 30, 30),    'rojo'),
    ((250, 140, 20),   'naranja'),
    ((245, 225, 40),   'amarillo'),
    ((40, 170, 60),    'verde'),
    ((30, 70, 200),    'azul'),
    ((130, 40, 190),   'morado'),
    ((240, 120, 190),  'rosa'),
])
def test_colores_basicos(rgb, esperado):
    assert color_dominante(_plano(rgb)) == esperado


@pytest.mark.parametrize('rgb, esperado', [
    ((255, 255, 255), 'blanco'),
    ((240, 240, 238), 'blanco'),
    ((128, 128, 128), 'gris'),
    ((10, 10, 10),    'negro'),
    ((35, 33, 30),    'negro'),
])
def test_sin_color_se_decide_por_el_brillo(rgb, esperado):
    """Sin tono fiable manda la luminosidad: es lo que separa un jersey blanco
    de uno negro, que en tono son indistinguibles."""
    assert color_dominante(_plano(rgb)) == esperado


# ── Los casos que el histograma de identificacion no puede resolver ──────────

@pytest.mark.parametrize('rgb', [
    (110, 70, 35),     # marron medio
    (60, 40, 25),      # marron oscuro
    (200, 175, 140),   # beige
])
def test_los_tonos_calidos_apagados_son_marron(rgb):
    """Marron es naranja oscuro o apagado: sin mirar el brillo saldria naranja."""
    assert color_dominante(_plano(rgb)) == 'marron'


def test_el_cian_cuenta_como_azul():
    """Nadie describe una prenda como cian: se agrupa con el azul."""
    assert color_dominante(_plano((25, 180, 190))) == 'azul'


def test_el_azul_marino_sigue_siendo_azul():
    """Oscuro no es lo mismo que acromatico: el tono todavia manda."""
    assert color_dominante(_plano((20, 30, 80))) == 'azul'


# ── Fotos menos limpias ───────────────────────────────────────────────────────

def test_el_fondo_de_los_bordes_no_decide():
    """Se recorta el centro a proposito: la cama o la mesa de debajo son ruido."""
    img = Image.new('RGB', (400, 400), (255, 255, 255))     # fondo blanco
    ImageDraw.Draw(img).rectangle([120, 120, 280, 280], fill=(30, 70, 200))

    assert color_dominante(img) == 'azul'


def test_una_prenda_con_estampado_devuelve_el_color_que_mas_pesa():
    """Rayas anchas azules sobre blanco: la prenda es azul."""
    img = Image.new('RGB', (200, 200), (255, 255, 255))
    dibujo = ImageDraw.Draw(img)
    for x in range(0, 200, 20):
        dibujo.rectangle([x, 0, x + 13, 200], fill=(30, 70, 200))

    assert color_dominante(img) == 'azul'


# ── Contrato ──────────────────────────────────────────────────────────────────

def test_siempre_devuelve_un_color_del_catalogo():
    """Lo que salga tiene que poder etiquetarse y filtrarse."""
    for rgb in [(0, 0, 0), (255, 255, 255), (123, 45, 67), (7, 200, 140),
                (180, 180, 40), (90, 90, 200), (255, 0, 255)]:
        assert color_dominante(_plano(rgb)) in BELONGING_COLORS


def test_no_depende_del_modelo_de_imagen(monkeypatch):
    """El color se calcula aunque no haya modelo ONNX instalado."""
    import app.image_match as im
    monkeypatch.setattr(im, '_load_session', lambda: None)

    assert color_dominante(_plano((40, 170, 60))) == 'verde'
