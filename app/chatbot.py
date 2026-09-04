"""
Chatbot IA basado en Claude API con tool use para consultas sobre la aplicación.
"""
from __future__ import annotations
import json
import unicodedata
from datetime import datetime, timedelta
from anthropic import Anthropic
from . import db
from .models import (Resident, CareRecord, CareType, CleaningRecord, Room, Floor,
                     Cleaner, ResidentGroup, ChecklistItem, ResidentDocument,
                     VitalSignType, VitalSignReading, AssessmentRecord, Incident)

SYSTEM_PROMPT = """Eres un asistente de la residencia de mayores "La Vila Gran".
Ayudas al personal a consultar información sobre residentes, limpiezas y atenciones.
Responde siempre en español, de forma breve y clara.
Usa las herramientas disponibles para buscar datos antes de responder.
Si no encuentras datos, dilo honestamente.
Formatea listas con viñetas y destaca nombres en negrita cuando sea útil.
No inventes datos — solo responde con lo que devuelven las herramientas.

Capacidades importantes:
- Los tipos de atención se organizan en categorías con subtipos: "Deposiciones" tiene FECALOMA, NORMAL y DIARREA; en el registro se guarda el subtipo. Las herramientas te lo devuelven como "Deposiciones: DIARREA".
- Para "quién ha hecho deposiciones", "a quién se le ha cambiado el pañal" o "cuántas diarreas ha habido", usa atenciones_por_tipo. Acepta tanto la categoría (Deposiciones) como el subtipo (DIARREA), y un periodo de varios días con el parámetro dias.
- Si atenciones_por_tipo no encuentra el tipo, mira los nombres reales con tipos_de_atencion y vuelve a intentarlo; no des por hecho que no ha pasado nada.
- Puedes buscar qué residentes tienen documentos adjuntos (PIAs, informes médicos, etc.) con residentes_con_documentos.
- Cuando pides info de un residente con info_residente, ya incluye el CONTENIDO de sus documentos (PIAs, informes). No necesitas llamar a leer_documento_residente por separado.
- Puedes responder preguntas sobre el contenido de los documentos (medicación, dietas, objetivos, etc.) directamente con la info que devuelve info_residente.
- Si el usuario pregunta "quién tiene PIA" o "qué residentes tienen informes", usa residentes_con_documentos.
- Puedes consultar constantes vitales (tensión arterial, glucemia, temperatura, etc.) de un residente con constantes_vitales_residente.
- Para preguntas como "cuál es la tensión de María" o "glucemia de Juan", usa constantes_vitales_residente.
- Para preguntas como "cómo está María" o "estado de Juan", usa info_residente — incluye perfil médico, valoraciones Barthel/Norton, notas recientes del personal e incidencias. Resume el estado del residente de forma clara.
- Si preguntan sobre valoraciones, dependencia o riesgo de úlceras, la info ya viene en info_residente (campo valoraciones).
- Puedes consultar turnos de cualquier día con consultar_turnos. Para "quién trabaja hoy", "quién está de tarde", etc.
- Si alguien falta o necesitan cobertura, usa sugerir_cobertura para encontrar al mejor candidato. Analiza horas, descansos y equidad.
- Para preguntas como "quién puede cubrir mañana por la mañana" o "María está de baja, quién la sustituye", usa sugerir_cobertura."""

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
                "dias": {"type": "integer", "description": "Cuántos días hacia atrás mirar, contando el de la fecha (por defecto 1, solo ese día)"}
            },
            "required": ["tipo"]
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


# ── Tipos de atención: la categoría importa tanto como el subtipo ─────────────
# "Deposiciones" tiene tres hijos (NORMAL, FECALOMA, DIARREA) y en el registro
# solo se guarda el hijo. Devolviendo "DIARREA" a secas, a la pregunta "quién ha
# hecho deposiciones" no se puede responder: el nombre no dice de qué es.

def _normaliza(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar lo que escribe el usuario."""
    sin_tildes = unicodedata.normalize('NFKD', texto or '')
    return ''.join(c for c in sin_tildes if not unicodedata.combining(c)).lower().strip()


def _nombre_tipo(ct: CareType) -> str:
    """'Deposiciones: DIARREA' para los subtipos, el nombre a secas si no lo es."""
    return f'{ct.parent.name}: {ct.name}' if ct.parent else ct.name


def _tipos_de(record: CareRecord) -> str:
    """Los tipos de un registro, con su categoría delante."""
    if record.care_types:
        return ', '.join(_nombre_tipo(ct) for ct in record.care_types)
    if record.care_type:
        return _nombre_tipo(record.care_type)
    return 'Sin tipo'


def _tipos_que_coinciden(termino: str) -> list[CareType]:
    """Tipos cuyo nombre —o el de su categoría— contiene el término buscado.

    Se filtra en Python y no con `ilike` para que las tildes den igual y para no
    depender del motor: son unas pocas decenas de tipos.
    """
    buscado = _normaliza(termino)
    if not buscado:
        return []
    return [ct for ct in CareType.query.all()
            if buscado in _normaliza(ct.name)
            or (ct.parent and buscado in _normaliza(ct.parent.name))]


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


def _extract_doc_text(doc) -> str:
    """Extract text content from a document file."""
    import os
    from flask import current_app
    full_path = os.path.join(current_app.config['UPLOAD_FOLDER'], doc.file_path)
    if not os.path.exists(full_path):
        return "(archivo no encontrado)"
    ext = doc.original_filename.rsplit('.', 1)[-1].lower() if '.' in doc.original_filename else ''
    if ext == 'pdf':
        try:
            import PyPDF2
            with open(full_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            return text.strip()[:4000] if text.strip() else "(PDF escaneado sin texto extraíble)"
        except Exception:
            return "(error al leer PDF)"
    elif ext in ('txt', 'csv', 'md'):
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read(4000)
    return f"(archivo .{ext} — no se puede leer texto)"


def _info_residente(residente_id: int, include_content: bool = False) -> str:
    r = db.session.get(Resident, residente_id)
    if not r:
        return json.dumps({"error": "Residente no encontrado"})
    # Últimas 5 atenciones
    recientes = CareRecord.query.filter_by(resident_id=r.id).filter(
        CareRecord.end_time.isnot(None)
    ).order_by(CareRecord.start_time.desc()).limit(5).all()
    atenciones = []
    for c in recientes:
        types = _tipos_de(c)
        atenciones.append({
            "fecha": c.start_time.strftime('%d/%m/%Y %H:%M'),
            "tipo": types,
            "trabajador": c.worker.name if c.worker else '?',
            "duracion_min": round(c.calculate_duration() / 60, 1) if c.calculate_duration() else None,
        })
    # Documents — include content only for admins
    docs = []
    for d in (r.documents or [])[:3]:
        doc_data = {
            "id": d.id, "nombre": d.original_filename, "tipo": d.doc_type or 'Otros',
            "descripcion": d.description or '',
            "fecha": d.uploaded_at.strftime('%d/%m/%Y') if d.uploaded_at else '',
        }
        if include_content:
            doc_data["contenido"] = _extract_doc_text(d)
        docs.append(doc_data)
    # Medical profile
    dep_labels = {'autonomous': 'Autónomo', 'mild': 'Leve', 'moderate': 'Moderado',
                  'severe': 'Severo', 'total': 'Total'}
    perfil_medico = {}
    if r.diagnoses:
        perfil_medico['diagnosticos'] = r.diagnoses
    if r.allergies:
        perfil_medico['alergias'] = r.allergies
    if r.current_medication:
        perfil_medico['medicacion'] = r.current_medication
    if r.blood_type:
        perfil_medico['grupo_sanguineo'] = r.blood_type
    if r.dependency_level:
        perfil_medico['nivel_dependencia'] = dep_labels.get(r.dependency_level, r.dependency_level)

    # Latest assessments (Barthel, Norton)
    valoraciones = {}
    for scale in ['barthel', 'norton']:
        latest = AssessmentRecord.query.filter_by(
            resident_id=r.id, scale_type=scale,
        ).order_by(AssessmentRecord.assessed_at.desc()).first()
        if latest:
            valoraciones[scale] = {
                'puntuacion': latest.score,
                'interpretacion': latest.interpretation,
                'fecha': latest.assessed_at.strftime('%d/%m/%Y'),
            }

    # Recent worker notes (last 7 days)
    cutoff_notes = datetime.now() - timedelta(days=7)
    recent_notes = []
    noted_records = CareRecord.query.filter(
        CareRecord.resident_id == r.id,
        CareRecord.notes.isnot(None),
        CareRecord.notes != '',
        CareRecord.start_time >= cutoff_notes,
    ).order_by(CareRecord.start_time.desc()).limit(5).all()
    for nr in noted_records:
        recent_notes.append({
            'fecha': nr.start_time.strftime('%d/%m'),
            'nota': nr.notes,
            'trabajador': nr.worker.name if nr.worker else '?',
        })

    # Recent incidents
    recent_incidents = Incident.query.filter(
        Incident.resident_id == r.id,
        Incident.created_at >= cutoff_notes,
    ).order_by(Incident.created_at.desc()).limit(3).all()
    incidencias = [{
        'fecha': i.created_at.strftime('%d/%m'),
        'titulo': i.title,
        'severidad': i.severity,
        'estado': i.status,
    } for i in recent_incidents]

    result = {
        "id": r.id, "nombre": r.name, "habitacion": r.room_number or "Sin asignar",
        "grupo": r.group.name if r.group else "Sin grupo",
        "notas": r.notes or "", "info_relevante": r.relevant_info or "",
        "activo": r.active, "tiene_foto": bool(r.photo_path),
        "ultimas_atenciones": atenciones,
        "documentos": docs,
    }
    if perfil_medico:
        result['perfil_medico'] = perfil_medico
    if valoraciones:
        result['valoraciones'] = valoraciones
    if recent_notes:
        result['notas_recientes_trabajadores'] = recent_notes
    if incidencias:
        result['incidencias_recientes'] = incidencias

    return json.dumps(result, ensure_ascii=False)


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
            "tipo": _tipos_de(c),
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
            "tipo": _tipos_de(c),
            "trabajador": c.worker.name if c.worker else '?',
            "en_curso": c.end_time is None,
        })
    return json.dumps({
        "fecha": hoy.strftime('%d/%m/%Y'),
        "total_atenciones": len(records),
        "por_residente": por_residente,
    }, ensure_ascii=False)


def _atenciones_por_tipo(tipo: str, fecha: str | None = None, dias: int = 1) -> str:
    coincidencias = _tipos_que_coinciden(tipo)
    if not coincidencias:
        return json.dumps({
            "error": f'No hay ningún tipo de atención que coincida con "{tipo}"',
            "tipos_disponibles": [_nombre_tipo(ct) for ct in
                                  CareType.query.filter_by(active=True).all()],
        }, ensure_ascii=False)

    dia = datetime.strptime(fecha, '%Y-%m-%d').date() if fecha else datetime.now().date()
    dias = max(1, int(dias or 1))
    inicio = datetime.combine(dia - timedelta(days=dias - 1), datetime.min.time())
    fin = datetime.combine(dia + timedelta(days=1), datetime.min.time())

    ids = {ct.id for ct in coincidencias}
    records = CareRecord.query.filter(
        CareRecord.start_time >= inicio, CareRecord.start_time < fin,
        # care_type_id es el campo antiguo: los registros de antes de los tipos
        # múltiples solo lo tienen a él.
        db.or_(CareRecord.care_types.any(CareType.id.in_(ids)),
               CareRecord.care_type_id.in_(ids)),
    ).order_by(CareRecord.start_time).all()

    atenciones = [{
        "residente": c.resident.name if c.resident else '?',
        "habitacion": (c.resident.room_number or 'Sin asignar') if c.resident else '?',
        "fecha": c.start_time.strftime('%d/%m/%Y'),
        "hora": c.start_time.strftime('%H:%M'),
        # Solo los tipos que se preguntaban: en un registro con varios, listarlos
        # todos haria pensar que la pregunta era por ellos.
        "tipo": ', '.join(_nombre_tipo(ct) for ct in c.care_types if ct.id in ids)
                or (_nombre_tipo(c.care_type) if c.care_type else 'Sin tipo'),
        "trabajador": c.worker.name if c.worker else '?',
        "notas": c.notes or None,
    } for c in records]

    return json.dumps({
        "buscado": tipo,
        "tipos_encontrados": [_nombre_tipo(ct) for ct in coincidencias],
        "periodo": (f"{inicio.strftime('%d/%m/%Y')} a {dia.strftime('%d/%m/%Y')}"
                    if dias > 1 else dia.strftime('%d/%m/%Y')),
        "total": len(atenciones),
        "residentes_distintos": len({a["residente"] for a in atenciones}),
        "atenciones": atenciones,
    }, ensure_ascii=False)


def _tipos_de_atencion() -> str:
    padres = CareType.query.filter_by(parent_id=None, active=True).order_by(
        CareType.sort_order, CareType.name).all()
    return json.dumps({"tipos": [{
        "nombre": p.name,
        "subtipos": [h.name for h in p.children if h.active],
    } for p in padres]}, ensure_ascii=False)


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


def _residentes_con_documentos(tipo: str | None = None) -> str:
    query = ResidentDocument.query
    if tipo:
        query = query.filter(ResidentDocument.doc_type.ilike(f'%{tipo}%'))
    docs = query.order_by(ResidentDocument.uploaded_at.desc()).all()
    if not docs:
        return json.dumps({"resultado": f"No se encontraron residentes con documentos{' de tipo ' + tipo if tipo else ''}"}, ensure_ascii=False)
    # Group by resident
    by_resident: dict[int, dict] = {}
    for d in docs:
        r = d.resident
        if not r:
            continue
        if r.id not in by_resident:
            by_resident[r.id] = {
                "id": r.id, "nombre": r.name,
                "habitacion": r.room_number or "Sin asignar",
                "documentos": [],
            }
        by_resident[r.id]["documentos"].append({
            "id": d.id, "nombre": d.original_filename,
            "tipo": d.doc_type or 'Otros',
            "descripcion": d.description or '',
            "fecha": d.uploaded_at.strftime('%d/%m/%Y') if d.uploaded_at else '',
        })
    return json.dumps({"residentes": list(by_resident.values())}, ensure_ascii=False)


def _leer_documento_residente(documento_id: int) -> str:
    import os
    from flask import current_app
    doc = db.session.get(ResidentDocument, documento_id)
    if not doc:
        return json.dumps({"error": "Documento no encontrado"})
    full_path = os.path.join(current_app.config['UPLOAD_FOLDER'], doc.file_path)
    if not os.path.exists(full_path):
        return json.dumps({"error": "Archivo no encontrado en disco"})
    ext = doc.original_filename.rsplit('.', 1)[-1].lower() if '.' in doc.original_filename else ''
    if ext == 'pdf':
        try:
            import PyPDF2
            with open(full_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            if not text.strip():
                return json.dumps({"residente": doc.resident.name, "documento": doc.original_filename, "contenido": "(PDF sin texto extraíble — puede ser un escaneo/imagen)"}, ensure_ascii=False)
            return json.dumps({"residente": doc.resident.name, "documento": doc.original_filename, "tipo": doc.doc_type, "contenido": text[:8000]}, ensure_ascii=False)
        except ImportError:
            return json.dumps({"error": "PyPDF2 no está instalado para leer PDFs"})
    elif ext in ('txt', 'csv', 'md'):
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read(8000)
        return json.dumps({"residente": doc.resident.name, "documento": doc.original_filename, "tipo": doc.doc_type, "contenido": text}, ensure_ascii=False)
    else:
        return json.dumps({"residente": doc.resident.name, "documento": doc.original_filename, "tipo": doc.doc_type, "nota": f"Archivo de tipo .{ext} — no se puede leer el contenido textual"}, ensure_ascii=False)


def _constantes_vitales_residente(residente_id: int, dias: int = 7, tipo: str = '') -> str:
    r = db.session.get(Resident, residente_id)
    if not r:
        return json.dumps({"error": "Residente no encontrado"})
    desde = datetime.now() - timedelta(days=dias)
    query = VitalSignReading.query.join(CareRecord).filter(
        CareRecord.resident_id == residente_id,
        VitalSignReading.recorded_at >= desde,
    ).join(VitalSignType)
    if tipo:
        query = query.filter(VitalSignType.name.ilike(f'%{tipo}%'))
    readings = query.order_by(VitalSignReading.recorded_at.desc()).limit(50).all()
    if not readings:
        return json.dumps({"residente": r.name, "resultado": f"No hay constantes vitales registradas en los últimos {dias} días"}, ensure_ascii=False)
    return json.dumps({
        "residente": r.name,
        "periodo": f"Últimos {dias} días",
        "total": len(readings),
        "lecturas": [{
            "tipo": rd.vital_sign_type.name,
            "valor": rd.value,
            "unidad": rd.vital_sign_type.unit,
            "fecha": rd.recorded_at.strftime('%d/%m/%Y %H:%M'),
            "trabajador": rd.care_record.worker.name if rd.care_record.worker else '?',
        } for rd in readings],
    }, ensure_ascii=False)


def _consultar_turnos(fecha_str: str = '') -> str:
    from .models import ShiftType, ShiftAssignment, Absence
    from datetime import date as _date
    try:
        target = _date.fromisoformat(fecha_str) if fecha_str else _date.today()
    except ValueError:
        target = _date.today()

    shift_types = {st.id: st for st in ShiftType.query.filter_by(active=True).all()}
    assignments = ShiftAssignment.query.filter_by(date=target).all()
    absences = Absence.query.filter(
        Absence.start_date <= target, Absence.end_date >= target
    ).all()
    absent_ids = {a.cleaner_id for a in absences}
    absent_names = []
    for a in absences:
        w = db.session.get(Cleaner, a.cleaner_id)
        reason = a.absence_type.name if a.absence_type else '?'
        absent_names.append(f"{w.name if w else '?'} ({reason})")

    turnos = {}
    for a in assignments:
        st = shift_types.get(a.shift_type_id)
        if not st:
            continue
        key = f"{st.short_name} ({st.name})"
        w = db.session.get(Cleaner, a.cleaner_id)
        w_name = w.name if w else '?'
        is_absent = a.cleaner_id in absent_ids
        turnos.setdefault(key, []).append(f"{w_name}{'  ⚠️ABSENT' if is_absent else ''}")

    return json.dumps({
        "fecha": target.isoformat(),
        "dia_semana": ['Dilluns','Dimarts','Dimecres','Dijous','Divendres','Dissabte','Diumenge'][target.weekday()],
        "turnos": turnos,
        "ausentes": absent_names,
        "total_asignados": len(assignments),
    }, ensure_ascii=False)


def _sugerir_cobertura(fecha_str: str, turno: str = '', motivo: str = '') -> str:
    from .models import ShiftType, ShiftAssignment, Absence
    from datetime import date as _date
    try:
        target = _date.fromisoformat(fecha_str)
    except ValueError:
        target = _date.today()

    shift_types = {st.short_name.lower(): st for st in ShiftType.query.filter_by(active=True).all()}
    shift_types_by_id = {st.id: st for st in ShiftType.query.filter_by(active=True).all()}

    # Find target shift type
    target_st = None
    if turno:
        target_st = shift_types.get(turno.lower())

    # All active workers
    workers = Cleaner.query.filter_by(active=True).filter(Cleaner.role != 'gestion').all()

    # Who is already assigned on that date
    assigned_ids = {a.cleaner_id for a in ShiftAssignment.query.filter_by(date=target).all()}

    # Who is absent
    absences = Absence.query.filter(
        Absence.start_date <= target, Absence.end_date >= target
    ).all()
    absent_ids = {a.cleaner_id for a in absences}

    # Calculate hours worked this week
    week_start = target - timedelta(days=target.weekday())
    week_end = week_start + timedelta(days=6)
    week_assignments = ShiftAssignment.query.filter(
        ShiftAssignment.date >= week_start,
        ShiftAssignment.date <= week_end,
    ).all()
    hours_this_week = {}
    for a in week_assignments:
        st = shift_types_by_id.get(a.shift_type_id)
        if st:
            h = ((datetime.combine(_date.today(), st.end_time) - datetime.combine(_date.today(), st.start_time)).total_seconds() / 3600)
            if h < 0:
                h += 24
            hours_this_week[a.cleaner_id] = hours_this_week.get(a.cleaner_id, 0) + h

    # Days worked consecutively before target date
    def consecutive_days(worker_id):
        count = 0
        d = target - timedelta(days=1)
        while count < 7:
            has = ShiftAssignment.query.filter_by(date=d, cleaner_id=worker_id).first()
            if not has:
                break
            count += 1
            d -= timedelta(days=1)
        return count

    # Check rest between shifts (day before)
    day_before = target - timedelta(days=1)
    prev_assignments = {a.cleaner_id: shift_types_by_id.get(a.shift_type_id)
                        for a in ShiftAssignment.query.filter_by(date=day_before).all()}

    candidates = []
    for w in workers:
        if w.id in assigned_ids or w.id in absent_ids:
            continue
        h = hours_this_week.get(w.id, 0)
        consec = consecutive_days(w.id)

        # Check minimum rest
        rest_ok = True
        prev_st = prev_assignments.get(w.id)
        if prev_st and target_st:
            prev_end_h = prev_st.end_time.hour + prev_st.end_time.minute / 60
            target_start_h = target_st.start_time.hour + target_st.start_time.minute / 60
            rest_hours = target_start_h - prev_end_h + 24 if target_start_h <= prev_end_h else target_start_h - prev_end_h
            if rest_hours < 11:
                rest_ok = False

        if consec >= 6:
            continue  # Max 6 consecutive days

        candidates.append({
            "nombre": w.name,
            "id": w.id,
            "horas_semana": round(h, 1),
            "dias_consecutivos": consec,
            "descanso_suficiente": rest_ok,
            "rol": w.role or 'mixto',
        })

    # Sort: fewer hours first, then fewer consecutive days
    candidates.sort(key=lambda c: (not c['descanso_suficiente'], c['horas_semana'], c['dias_consecutivos']))

    return json.dumps({
        "fecha": target.isoformat(),
        "turno_solicitado": turno or 'no especificado',
        "motivo": motivo,
        "candidatos": candidates[:5],
        "total_disponibles": len(candidates),
    }, ensure_ascii=False)


def _get_tool_handlers(is_admin: bool = False):
    handlers = {
        "buscar_residente": lambda args: _buscar_residente(args["nombre"]),
        "info_residente": lambda args: _info_residente(args["residente_id"], include_content=is_admin),
        "atenciones_residente": lambda args: _atenciones_residente(args["residente_id"], args.get("fecha")),
        "atenciones_hoy": lambda args: _atenciones_hoy(),
        "atenciones_por_tipo": lambda args: _atenciones_por_tipo(args["tipo"], args.get("fecha"), args.get("dias", 1)),
        "tipos_de_atencion": lambda args: _tipos_de_atencion(),
        "limpiezas_hoy": lambda args: _limpiezas_hoy(),
        "ultima_limpieza_zona": lambda args: _ultima_limpieza_zona(args["numero_zona"]),
        "trabajadores_activos": lambda args: _trabajadores_activos(),
        "resumen_dia": lambda args: _resumen_dia(args.get("fecha")),
        "buscar_trabajador": lambda args: _buscar_trabajador(args["nombre"]),
        "residentes_con_documentos": lambda args: _residentes_con_documentos(args.get("tipo")),
        "constantes_vitales_residente": lambda args: _constantes_vitales_residente(args["residente_id"], args.get("dias", 7), args.get("tipo", '')),
        "consultar_turnos": lambda args: _consultar_turnos(args.get("fecha", '')),
        "sugerir_cobertura": lambda args: _sugerir_cobertura(args["fecha"], args.get("turno", ''), args.get("motivo", '')),
    }
    if is_admin:
        handlers["leer_documento_residente"] = lambda args: _leer_documento_residente(args["documento_id"])
    return handlers


def chat(message: str, api_key: str, is_admin: bool = False) -> str:
    """Procesa un mensaje del usuario y devuelve la respuesta del chatbot."""
    client = Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": message}]
    handlers = _get_tool_handlers(is_admin)
    # Filter tools to only those available for this role
    available_tools = [t for t in TOOLS if t['name'] in handlers]

    # Primera llamada — Claude puede pedir tools
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=available_tools,
        messages=messages,
    )

    # Loop de tool use — Claude puede llamar varias herramientas
    while response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = handlers.get(block.name)
                result = handler(block.input) if handler else json.dumps({"error": "Herramienta no disponible"})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=available_tools,
            messages=messages,
        )

    # Extraer texto de la respuesta final
    text_parts = [block.text for block in response.content if hasattr(block, 'text')]
    return '\n'.join(text_parts) or "No he podido generar una respuesta."
