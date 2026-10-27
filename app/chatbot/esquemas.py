"""Las definiciones de las herramientas que ve el modelo.

Viven en un fichero aparte y en **orden fijo** a proposito: estos bytes van en
la posicion 0 del prefijo que se cachea en cada peticion, asi que montar la
lista iterando modulos o un set la reordenaria y tiraria la cache sin que nadie
se entere. Un fichero, una lista, un orden.
"""
from __future__ import annotations



TOOLS = [
    {
        "name": "buscar_residente",
        "description": "Busca residentes por nombre. Acepta el nombre incompleto, las palabras en cualquier orden (nombre y apellidos dan igual), sin tildes y con erratas leves. Devuelve lista de coincidencias con id, nombre, habitación y grupo; si no encuentra a nadie, devuelve nombres parecidos en sugerencias.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del residente, entero o en parte, en el orden que sea"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "info_residente",
        "description": "Devuelve información completa de un residente: nombre, habitación, grupo, notas, información relevante y últimas atenciones.",
        "input_schema": {
            "type": "object",
            "properties": {
                "residente_id": {"type": "integer", "description": "ID del residente"}
            },
            "required": ["residente_id"]
        }
    },
    {
        "name": "atenciones_residente",
        "description": "Lista las atenciones/asistencias realizadas a un residente en una fecha. Si no se indica fecha, usa hoy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "residente_id": {"type": "integer", "description": "ID del residente"},
                "fecha": {"type": "string", "description": "Fecha en formato YYYY-MM-DD (opcional, por defecto hoy)"}
            },
            "required": ["residente_id"]
        }
    },
    {
        "name": "atenciones_hoy",
        "description": "Lista todas las atenciones realizadas hoy en la residencia, agrupadas por residente.",
        "input_schema": {
            "type": "object",
            "properties": {},
        }
    },
    {
        "name": "atenciones_por_tipo",
        "description": (
            "Busca qué residentes han recibido un tipo de atención concreto y cuándo. "
            "El término puede ser una categoría (ej: Deposiciones, Higiene) o un subtipo "
            "concreto (ej: DIARREA, FECALOMA): si es una categoría, devuelve todos sus "
            "subtipos. Úsala para preguntas del tipo 'quién ha hecho deposiciones', "
            "'a quién se le ha cambiado el pañal', 'cuántas diarreas ha habido esta semana'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "description": "Nombre de la categoría o del subtipo de atención a buscar"},
                "fecha": {"type": "string", "description": "Día final del periodo, en formato YYYY-MM-DD (opcional, por defecto hoy)"},
                "dias": {"type": "integer", "description": "Cuántos días hacia atrás mirar, contando el de la fecha (por defecto 1, solo ese día)"},
                "desde_hora": {"type": "string", "description": "Hora de inicio de la franja, HH:MM o HH (opcional). Solo tiene sentido con dias=1"},
                "hasta_hora": {"type": "string", "description": "Hora de fin de la franja, HH:MM o HH (opcional)"}
            },
            "required": ["tipo"]
        }
    },
    {
        "name": "residentes_sin_atencion",
        "description": ("Residentes que NO han recibido un tipo de atención en un día, opcionalmente "
                        "dentro de una franja horaria. Para preguntas en negativo: \"a quién no se le "
                        "ha cambiado el pañal entre las 9 y las 13\", \"quién no ha desayunado\". "
                        "De cada residente sin la atención dice cuándo la recibió por última vez, que "
                        "es lo que distingue a quien se ha quedado sin ella de quien no la necesita."),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "description": "Tipo o categoría de atención (ej. 'Cambio de pañal', 'Deposiciones')"},
                "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD (opcional, por defecto hoy)"},
                "desde_hora": {"type": "string", "description": "Hora de inicio de la franja, HH:MM o HH (opcional)"},
                "hasta_hora": {"type": "string", "description": "Hora de fin de la franja, HH:MM o HH (opcional)"},
            },
            "required": ["tipo"],
        }
    },
    {
        "name": "tipos_de_atencion",
        "description": (
            "Lista los tipos de atención configurados, con sus subtipos. Úsala cuando no "
            "sepas con qué nombre se registra algo, o cuando atenciones_por_tipo no "
            "encuentre coincidencias."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        }
    },
    {
        "name": "limpiezas_hoy",
        "description": "Muestra qué zonas se han limpiado hoy y cuáles están pendientes.",
        "input_schema": {
            "type": "object",
            "properties": {},
        }
    },
    {
        "name": "ultima_limpieza_zona",
        "description": "Muestra cuándo fue la última limpieza de una zona/habitación específica.",
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_zona": {"type": "string", "description": "Número de la zona/habitación"}
            },
            "required": ["numero_zona"]
        }
    },
    {
        "name": "trabajadores_activos",
        "description": "Muestra qué trabajadores están realizando alguna actividad ahora mismo (limpieza o atención en curso).",
        "input_schema": {
            "type": "object",
            "properties": {},
        }
    },
    {
        "name": "uso_de_la_aplicacion",
        "description": ("Cuánto usa cada trabajadora la webapp del móvil en un periodo: veces que ha entrado, "
                        "limpiezas y atenciones que ha registrado, y cuándo se la vio por última vez. "
                        "Devuelve también los días de turno y los días de ausencia de cada una, que hacen falta "
                        "para interpretar los ceros. La lista viene ordenada de menos uso a más uso."),
        "input_schema": {
            "type": "object",
            "properties": {
                "dias": {"type": "integer", "description": "Días hacia atrás que se miran (por defecto 30)"}
            },
        }
    },
    {
        "name": "resumen_dia",
        "description": "Resumen general del día: total limpiezas, total atenciones, trabajadores activos, zonas pendientes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {"type": "string", "description": "Fecha en formato YYYY-MM-DD (opcional, por defecto hoy)"}
            },
        }
    },
    {
        "name": "buscar_trabajador",
        "description": "Busca trabajadoras por nombre. Acepta el nombre incompleto, las palabras en cualquier orden, sin tildes y con erratas leves. Si no encuentra a nadie, devuelve nombres parecidos en sugerencias.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la trabajadora, entero o en parte, en el orden que sea"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "leer_documento_residente",
        "description": "Lee el contenido de un documento de un residente (PIA, informe médico, etc). Normalmente no necesitas usar esto porque info_residente ya incluye el contenido.",
        "input_schema": {
            "type": "object",
            "properties": {
                "documento_id": {"type": "integer", "description": "ID del documento a leer"}
            },
            "required": ["documento_id"]
        }
    },
    {
        "name": "residentes_con_documentos",
        "description": "Lista los residentes que tienen documentos adjuntos (PIAs, informes médicos, etc). Puede filtrar por tipo de documento.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "description": "Filtrar por tipo: PIA, Informe médico, Pauta farmacológica, Consentimiento, Otros. Dejar vacío para todos."}
            },
        }
    },
    {
        "name": "constantes_vitales_residente",
        "description": "Consulta las constantes vitales registradas para un residente (tensión arterial, glucemia, temperatura, etc). Puede filtrar por tipo y número de días.",
        "input_schema": {
            "type": "object",
            "properties": {
                "residente_id": {"type": "integer", "description": "ID del residente"},
                "dias": {"type": "integer", "description": "Últimos N días (por defecto 7)"},
                "tipo": {"type": "string", "description": "Filtrar por nombre del campo vital (ej: Sistólica, Glucosa). Vacío para todos."}
            },
            "required": ["residente_id"]
        }
    },
    {
        "name": "consultar_turnos",
        "description": "Consulta los turnos asignados en una fecha. Muestra qué trabajadores trabajan, en qué turno, y quién está ausente. Si no se indica fecha se usa hoy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {"type": "string", "description": "Fecha en formato YYYY-MM-DD. Si no se indica, se usa hoy."}
            },
        }
    },
    {
        "name": "sugerir_cobertura",
        "description": "Sugiere el mejor trabajador para cubrir un turno vacante o sustituir a un ausente. Analiza horas trabajadas, descansos, competencias y equidad.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {"type": "string", "description": "Fecha del turno a cubrir (YYYY-MM-DD)"},
                "turno": {"type": "string", "description": "Nombre corto del turno (ej: M, T, N1). Si no se sabe, dejarlo vacío."},
                "motivo": {"type": "string", "description": "Motivo de la búsqueda (ej: 'María está de baja', 'falta 1 persona por la tarde')"}
            },
            "required": ["fecha"]
        }
    },
]
