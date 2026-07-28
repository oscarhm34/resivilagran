"""
Chatbot IA basado en Claude API con tool use para consultas sobre la aplicación.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from anthropic import Anthropic
from . import db
from .models import (Resident, CareRecord, CareType, CleaningRecord, Room, Floor,
                     Cleaner, ResidentGroup, ChecklistItem)

SYSTEM_PROMPT = """Eres un asistente de la residencia de mayores "La Vila Gran".
Ayudas al personal a consultar información sobre residentes, limpiezas y atenciones.
Responde siempre en español, de forma breve y clara.
Usa las herramientas disponibles para buscar datos antes de responder.
Si no encuentras datos, dilo honestamente.
Formatea listas con viñetas y destaca nombres en negrita cuando sea útil.
No inventes datos — solo responde con lo que devuelven las herramientas."""

TOOLS = [
    {
        "name": "buscar_residente",
        "description": "Busca residentes por nombre (parcial). Devuelve lista de coincidencias con id, nombre, habitación y grupo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre parcial del residente a buscar"}
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
        "description": "Busca trabajadores por nombre parcial.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre parcial del trabajador"}
            },
            "required": ["nombre"]
        }
    },
]


# ── Tool implementations ──────────────────────────────────────────────────────

def _buscar_residente(nombre: str) -> str:
    results = Resident.query.filter(
        Resident.name.ilike(f'%{nombre}%'), Resident.active == True
    ).order_by(Resident.name).limit(10).all()
    if not results:
        return json.dumps({"resultado": "No se encontraron residentes con ese nombre"})
    return json.dumps({"residentes": [{
        "id": r.id, "nombre": r.name, "habitacion": r.room_number or "Sin asignar",
        "grupo": r.group.name if r.group else "Sin grupo",
    } for r in results]}, ensure_ascii=False)


def _info_residente(residente_id: int) -> str:
    r = db.session.get(Resident, residente_id)
    if not r:
        return json.dumps({"error": "Residente no encontrado"})
    # Últimas 5 atenciones
    recientes = CareRecord.query.filter_by(resident_id=r.id).filter(
        CareRecord.end_time.isnot(None)
    ).order_by(CareRecord.start_time.desc()).limit(5).all()
    atenciones = []
    for c in recientes:
        types = ', '.join(ct.name for ct in c.care_types) if c.care_types else (c.care_type.name if c.care_type else 'Sin tipo')
        atenciones.append({
            "fecha": c.start_time.strftime('%d/%m/%Y %H:%M'),
            "tipo": types,
            "trabajador": c.worker.name if c.worker else '?',
            "duracion_min": round(c.calculate_duration() / 60, 1) if c.calculate_duration() else None,
        })
    return json.dumps({
        "id": r.id, "nombre": r.name, "habitacion": r.room_number or "Sin asignar",
        "grupo": r.group.name if r.group else "Sin grupo",
        "notas": r.notes or "", "info_relevante": r.relevant_info or "",
        "activo": r.active, "tiene_foto": bool(r.photo_path),
        "ultimas_atenciones": atenciones,
    }, ensure_ascii=False)


def _atenciones_residente(residente_id: int, fecha: str | None = None) -> str:
    r = db.session.get(Resident, residente_id)
    if not r:
        return json.dumps({"error": "Residente no encontrado"})
    if fecha:
        dia = datetime.strptime(fecha, '%Y-%m-%d').date()
    else:
        dia = datetime.now().date()
    inicio = datetime.combine(dia, datetime.min.time())
    fin = datetime.combine(dia + timedelta(days=1), datetime.min.time())
    records = CareRecord.query.filter(
        CareRecord.resident_id == residente_id,
        CareRecord.start_time >= inicio, CareRecord.start_time < fin,
    ).order_by(CareRecord.start_time).all()
    return json.dumps({
        "residente": r.name, "fecha": dia.strftime('%d/%m/%Y'),
        "total": len(records),
        "atenciones": [{
            "hora": c.start_time.strftime('%H:%M'),
            "tipo": ', '.join(ct.name for ct in c.care_types) if c.care_types else (c.care_type.name if c.care_type else 'Sin tipo'),
            "trabajador": c.worker.name if c.worker else '?',
            "duracion_min": round(c.calculate_duration() / 60, 1) if c.calculate_duration() else None,
            "en_curso": c.end_time is None,
        } for c in records],
    }, ensure_ascii=False)


def _atenciones_hoy() -> str:
    hoy = datetime.now().date()
    inicio = datetime.combine(hoy, datetime.min.time())
    fin = datetime.combine(hoy + timedelta(days=1), datetime.min.time())
    records = CareRecord.query.filter(
        CareRecord.start_time >= inicio, CareRecord.start_time < fin,
    ).order_by(CareRecord.start_time).all()
    por_residente: dict[str, list] = {}
    for c in records:
        name = c.resident.name if c.resident else '?'
        por_residente.setdefault(name, []).append({
            "hora": c.start_time.strftime('%H:%M'),
            "tipo": ', '.join(ct.name for ct in c.care_types) if c.care_types else (c.care_type.name if c.care_type else 'Sin tipo'),
            "trabajador": c.worker.name if c.worker else '?',
            "en_curso": c.end_time is None,
        })
    return json.dumps({
        "fecha": hoy.strftime('%d/%m/%Y'),
        "total_atenciones": len(records),
        "por_residente": por_residente,
    }, ensure_ascii=False)


def _limpiezas_hoy() -> str:
    hoy = datetime.now().date()
    inicio = datetime.combine(hoy, datetime.min.time())
    fin = datetime.combine(hoy + timedelta(days=1), datetime.min.time())
    realizadas = CleaningRecord.query.filter(
        CleaningRecord.start_time >= inicio, CleaningRecord.start_time < fin,
    ).all()
    room_ids_limpiadas = {c.room_id for c in realizadas}
    todas_rooms = Room.query.order_by(Room.number).all()
    limpiadas = []
    pendientes = []
    for room in todas_rooms:
        info = {"numero": room.number, "descripcion": room.description or "",
                "planta": room.floor.name if room.floor else "?"}
        if room.id in room_ids_limpiadas:
            limpiadas.append(info)
        else:
            pendientes.append(info)
    return json.dumps({
        "fecha": hoy.strftime('%d/%m/%Y'),
        "limpiadas": len(limpiadas), "pendientes": len(pendientes),
        "zonas_limpiadas": limpiadas[:20],
        "zonas_pendientes": pendientes[:20],
        "nota": f"Mostrando max 20 de cada" if len(limpiadas) > 20 or len(pendientes) > 20 else "",
    }, ensure_ascii=False)


def _ultima_limpieza_zona(numero_zona: str) -> str:
    room = Room.query.filter_by(number=numero_zona).first()
    if not room and numero_zona.isdigit():
        room = Room.query.filter(Room.number.in_([numero_zona.zfill(4), numero_zona.zfill(5)])).first()
    if not room:
        return json.dumps({"error": f"Zona '{numero_zona}' no encontrada"})
    ultimo = CleaningRecord.query.filter_by(room_id=room.id).filter(
        CleaningRecord.end_time.isnot(None)
    ).order_by(CleaningRecord.end_time.desc()).first()
    if not ultimo:
        return json.dumps({"zona": room.number, "descripcion": room.description or "", "ultima_limpieza": "Nunca"})
    return json.dumps({
        "zona": room.number, "descripcion": room.description or "",
        "ultima_limpieza": ultimo.end_time.strftime('%d/%m/%Y %H:%M'),
        "trabajador": ultimo.cleaner.name if ultimo.cleaner else '?',
        "duracion_min": round(ultimo.calculate_duration() / 60, 1) if ultimo.calculate_duration() else None,
    }, ensure_ascii=False)


def _trabajadores_activos() -> str:
    activos: list[dict] = []
    for c in CleaningRecord.query.filter_by(end_time=None).all():
        room = c.room
        activos.append({
            "trabajador": c.cleaner.name if c.cleaner else '?',
            "tipo": "Limpieza",
            "detalle": f"Hab. {room.number}" if room else "?",
            "desde": c.start_time.strftime('%H:%M') if c.start_time else "?",
        })
    for c in CareRecord.query.filter_by(end_time=None).all():
        activos.append({
            "trabajador": c.worker.name if c.worker else '?',
            "tipo": "Atención",
            "detalle": c.resident.name if c.resident else "?",
            "desde": c.start_time.strftime('%H:%M') if c.start_time else "?",
        })
    return json.dumps({
        "total_activos": len(activos),
        "sesiones": activos,
    }, ensure_ascii=False)


def _resumen_dia(fecha: str | None = None) -> str:
    if fecha:
        dia = datetime.strptime(fecha, '%Y-%m-%d').date()
    else:
        dia = datetime.now().date()
    inicio = datetime.combine(dia, datetime.min.time())
    fin = datetime.combine(dia + timedelta(days=1), datetime.min.time())
    limpiezas = CleaningRecord.query.filter(
        CleaningRecord.start_time >= inicio, CleaningRecord.start_time < fin,
    ).count()
    atenciones = CareRecord.query.filter(
        CareRecord.start_time >= inicio, CareRecord.start_time < fin,
    ).count()
    activos_ahora = CleaningRecord.query.filter_by(end_time=None).count() + CareRecord.query.filter_by(end_time=None).count()
    total_rooms = Room.query.count()
    rooms_limpiadas = db.session.query(CleaningRecord.room_id).filter(
        CleaningRecord.start_time >= inicio, CleaningRecord.start_time < fin,
    ).distinct().count()
    return json.dumps({
        "fecha": dia.strftime('%d/%m/%Y'),
        "total_limpiezas": limpiezas,
        "total_atenciones": atenciones,
        "trabajadores_activos_ahora": activos_ahora,
        "zonas_limpiadas": rooms_limpiadas,
        "zonas_total": total_rooms,
        "zonas_pendientes": total_rooms - rooms_limpiadas,
    }, ensure_ascii=False)


def _buscar_trabajador(nombre: str) -> str:
    results = Cleaner.query.filter(
        Cleaner.name.ilike(f'%{nombre}%'), Cleaner.active == True
    ).order_by(Cleaner.name).limit(10).all()
    if not results:
        return json.dumps({"resultado": "No se encontraron trabajadores con ese nombre"})
    return json.dumps({"trabajadores": [{
        "id": c.id, "nombre": c.name, "usuario": c.username,
        "es_admin": c.is_admin, "grupos": [g.name for g in c.groups],
    } for c in results]}, ensure_ascii=False)


TOOL_HANDLERS = {
    "buscar_residente": lambda args: _buscar_residente(args["nombre"]),
    "info_residente": lambda args: _info_residente(args["residente_id"]),
    "atenciones_residente": lambda args: _atenciones_residente(args["residente_id"], args.get("fecha")),
    "atenciones_hoy": lambda args: _atenciones_hoy(),
    "limpiezas_hoy": lambda args: _limpiezas_hoy(),
    "ultima_limpieza_zona": lambda args: _ultima_limpieza_zona(args["numero_zona"]),
    "trabajadores_activos": lambda args: _trabajadores_activos(),
    "resumen_dia": lambda args: _resumen_dia(args.get("fecha")),
    "buscar_trabajador": lambda args: _buscar_trabajador(args["nombre"]),
}


def chat(message: str, api_key: str) -> str:
    """Procesa un mensaje del usuario y devuelve la respuesta del chatbot."""
    client = Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": message}]

    # Primera llamada — Claude puede pedir tools
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
    )

    # Loop de tool use — Claude puede llamar varias herramientas
    while response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                result = handler(block.input) if handler else json.dumps({"error": "Herramienta no encontrada"})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

    # Extraer texto de la respuesta final
    text_parts = [block.text for block in response.content if hasattr(block, 'text')]
    return '\n'.join(text_parts) or "No he podido generar una respuesta."
