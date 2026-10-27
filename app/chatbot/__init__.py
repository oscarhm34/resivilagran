"""Asistente IA de la residencia.

Era un modulo de 1.340 lineas. Se parte en paquete porque el catalogo de
herramientas va a crecer mucho, y porque dentro conviven tres clases de codigo
con ritmos de cambio distintos: la fontaneria de la API, el prompt, y medio
centenar de funciones de consulta independientes.

Lo que NO cambia es como se usa desde fuera: aqui se reexporta la superficie
publica entera, asi que `from ..chatbot import chat` y los imports de los tests
siguen funcionando exactamente igual que antes del reparto.

Los nombres con guion bajo no entran con `import *`, asi que van enumerados. La
lista se saco leyendo los imports reales del resto del proyecto: si alguna vez
falta uno, el test que lo importaba falla al cargar, que es justo lo que quieres
que pase.
"""
from __future__ import annotations

from .prompt import (  # noqa: F401
    SYSTEM_PROMPT,
    CHATBOT_EXTRA_KEY,
    MAX_EXTRA_PROMPT,
    system_prompt,
)
from .esquemas import TOOLS  # noqa: F401
from .nucleo import (  # noqa: F401
    chat,
    MAXIMO_RESULTADOS,
    _get_tool_handlers,
    # Busqueda de personas
    _buscar_residente,
    _buscar_trabajador,
    # Tipos de atencion
    _nombre_tipo,
    _tipos_de,
    _tipos_de_atencion,
    _tipos_que_coinciden,
    # Atenciones
    _atenciones_hoy,
    _atenciones_por_tipo,
    _residentes_sin_atencion,
    # Turnos y personal
    _sugerir_cobertura,
    _uso_de_la_aplicacion,
    # Fechas y horas
    _hora,
    _franja,
)
