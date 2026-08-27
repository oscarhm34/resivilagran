"""
test_limites_ia.py — Toda ruta que llame a la IA debe llevar limite.

Cada llamada a Anthropic o a OpenAI cuesta dinero, y algunas rutas recorren
toda la residencia: una pulsacion de mas se multiplica en la factura. Con 230
rutas y decoradores puestos a mano, esto no se puede vigilar de memoria.

El barrido recorre el `url_map`, se queda con las vistas de escritura cuyo
codigo llama a la IA y comprueba que todas tienen `@limiter.limit`. Una ruta de
IA nueva sin limite hace fallar este test sola.
"""

import inspect

import pytest

from app import app as flask_app, limiter

# Marcas de que la vista acaba llamando al modelo.
MARCAS_IA = ('_call_claude', 'Anthropic', 'anthropic',
             'OPENAI_API_KEY', 'openai', 'whisper')

# Exentas a proposito: llaman a la IA de pasada al cerrar una sesion de trabajo.
# Son el flujo normal de la trabajadora y no se pueden disparar en rafaga, asi
# que limitarlas romperia el trabajo real sin ahorrar nada.
EXENTAS = {
    'nfc.finalize_care',
    'nfc.finalize_cleaning',
}


def _clave_limiter(vista):
    """Flask-Limiter registra cada ruta como '<modulo>.<qualname>.<nombre>'."""
    return f'{vista.__module__}.{vista.__qualname__}.{vista.__name__}'


def _vistas_de_ia():
    """Rutas de escritura cuyo cuerpo llama a la IA."""
    encontradas = {}
    for rule in flask_app.url_map.iter_rules():
        if rule.endpoint in EXENTAS:
            continue
        if not (rule.methods or set()) & {'POST', 'PUT', 'PATCH'}:
            continue
        vista = flask_app.view_functions.get(rule.endpoint)
        if vista is None:
            continue
        try:
            codigo = inspect.getsource(vista)
        except (OSError, TypeError):
            continue
        if any(marca in codigo for marca in MARCAS_IA):
            encontradas[rule.endpoint] = vista
    return [pytest.param(e, id=e) for e in sorted(encontradas)]


VISTAS_DE_IA = _vistas_de_ia()


def test_el_barrido_encuentra_rutas_de_ia():
    """Guarda contra un detector que se quede sin nada que recorrer."""
    assert len(VISTAS_DE_IA) > 15


@pytest.mark.parametrize('endpoint', VISTAS_DE_IA)
def test_toda_ruta_de_ia_tiene_limite(endpoint):
    vista = flask_app.view_functions[endpoint]

    # `_decorated_limits` es interno de Flask-Limiter, pero es el unico registro
    # que dice si una vista concreta lleva decorador. Comprobado con 4.1.1.
    registrados = limiter.limit_manager._decorated_limits.get(_clave_limiter(vista))

    assert registrados, (
        f'{endpoint} llama a la IA y no tiene @limiter.limit. '
        f'Si la exencion es deliberada, anadela a EXENTAS explicando por que.'
    )


def test_las_exentas_siguen_existiendo():
    """Si se renombra una ruta exenta, la exencion deja de protegerla en silencio."""
    endpoints = {r.endpoint for r in flask_app.url_map.iter_rules()}

    assert EXENTAS <= endpoints, f'exentas que ya no existen: {EXENTAS - endpoints}'
