"""
Chatbot IA basado en Claude API con tool use para consultas sobre la aplicación.
"""
from __future__ import annotations
import difflib
import json
import re
import unicodedata
from datetime import datetime, timedelta, time as dt_time
from anthropic import Anthropic
from .. import db, app
from .prompt import (SYSTEM_PROMPT, CHATBOT_EXTRA_KEY, MAX_EXTRA_PROMPT,  # noqa: F401
                     system_prompt, _bloque_de_fecha)
from .esquemas import TOOLS
from .guias import GUIAS, TEMAS
from ..models import (Resident, CareRecord, CareType, CleaningRecord, Room, Floor,
                     Cleaner, ResidentGroup, ChecklistItem, ResidentDocument,
                     VitalSignType, VitalSignReading, AssessmentRecord, Incident,
                     WorkerDeviceLogin, ShiftAssignment, Absence, AppSetting)


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


# ── Buscar por nombre: ni el orden, ni las tildes, ni una errata deben estorbar ─
# Los nombres viven en un unico campo libre y sin criterio fijo: unos son "NOMBRE
# APELLIDOS" y otros "APELLIDOS, NOMBRE". Un `ilike '%texto%'` obliga a acertar el
# orden exacto, asi que "Tadea Cazorla" no encontraba a "CAZORLA CARRILLO TADEA"
# y "Assumpcio" no encontraba a "ASSUMPCIO ZAPATER" por la tilde.
#
# Se compara palabra a palabra sobre la lista entera —son ciento y pico— por el
# mismo motivo que en `_tipos_que_coinciden`: para que las tildes den igual y para
# no depender del motor (`unaccent` y `pg_trgm` no existen en el SQLite de los
# tests).

MAXIMO_RESULTADOS = 10   # mas que esto no ayuda a nadie a decidir
PARECIDO_MINIMO = 0.8    # a partir de aqui dos palabras se dan por la misma
LARGO_MINIMO_ERRATA = 4  # en palabras cortas un parecido alto no significa nada


def _palabras(texto: str) -> list[str]:
    """Las palabras de un nombre, normalizadas y sin signos de puntuacion.

    El split por lo no alfanumerico es lo que despega la coma de los nombres al
    estilo "ARMADA PUJOL, TERESA".
    """
    return [p for p in re.split(r'[^0-9a-z]+', _normaliza(texto)) if p]


def _encaja(escrita: str, palabra: str, erratas: bool) -> float:
    """Cuanto encaja una palabra escrita con una del nombre. 0.0 si no encaja.

    Basta con que sea un prefijo, para que "Tadea C" siga valiendo.
    """
    if palabra.startswith(escrita):
        return 1.0
    if not erratas or min(len(escrita), len(palabra)) < LARGO_MINIMO_ERRATA:
        return 0.0
    parecido = difflib.SequenceMatcher(None, escrita, palabra).ratio()
    return parecido if parecido >= PARECIDO_MINIMO else 0.0


def _puntua(escritas: list[str], nombre: str, erratas: bool) -> float:
    """Puntua un nombre frente a lo escrito; 0.0 si alguna palabra no encaja.

    Cada palabra escrita tiene que encajar con una palabra distinta del nombre,
    en el orden que sea. La media permite ordenar despues: quien encaja por
    prefijo limpio queda por delante de quien encaja arrastrando una errata.
    """
    disponibles = _palabras(nombre)
    if not escritas or not disponibles:
        return 0.0
    total = 0.0
    for escrita in escritas:
        mejor, cual = 0.0, None
        for i, palabra in enumerate(disponibles):
            punto = _encaja(escrita, palabra, erratas)
            if punto > mejor:
                mejor, cual = punto, i
        if not mejor:
            return 0.0
        total += mejor
        disponibles.pop(cual)
    return total / len(escritas)


def _parecidos(escritas: list[str], candidatos: list, cuantos: int = 5) -> list:
    """Los nombres que mas se parecen a lo escrito, aunque no lleguen a encajar.

    Para que el asistente pueda preguntar "¿te refieres a...?" en lugar de
    responder que ese residente no existe, que es otra cosa.
    """
    buscado = ' '.join(escritas)
    puntuados = []
    for c in candidatos:
        palabras = _palabras(c.name)
        mejor = difflib.SequenceMatcher(None, buscado, ' '.join(palabras)).ratio()
        for palabra in palabras:
            for escrita in escritas:
                mejor = max(mejor, difflib.SequenceMatcher(
                    None, escrita, palabra).ratio())
        puntuados.append((mejor, c))
    puntuados.sort(key=lambda p: (-p[0], p[1].name))
    return [c for parecido, c in puntuados[:cuantos] if parecido >= 0.5]


def _buscar_por_nombre(consulta: str, candidatos: list) -> tuple[list, list]:
    """Los candidatos que encajan con lo escrito y, si no hay ninguno, los parecidos.

    Devuelve `(coincidencias, sugerencias)`. Se prueba primero sin tolerar
    erratas y solo se repite admitiendolas si la primera pasada no da nada: asi
    una errata no ensucia los resultados cuando la busqueda limpia ya funcionaba.
    """
    escritas = _palabras(consulta)
    if not escritas or not candidatos:
        return [], []
    for erratas in (False, True):
        puntuados = [(_puntua(escritas, c.name, erratas), c) for c in candidatos]
        encajan = sorted((p for p in puntuados if p[0]),
                         key=lambda p: (-p[0], p[1].name))
        if encajan:
            return [c for _, c in encajan], []
    return [], _parecidos(escritas, candidatos)


# ── Tool implementations ──────────────────────────────────────────────────────

def _buscar_residente(nombre: str) -> str:
    activos = Resident.query.filter_by(active=True).order_by(Resident.name).all()
    encontrados, parecidos = _buscar_por_nombre(nombre, activos)

    if encontrados:
        respuesta = {"residentes": [{
            "id": r.id, "nombre": r.name,
            "habitacion": r.room_number or "Sin asignar",
            "grupo": r.group.name if r.group else "Sin grupo",
        } for r in encontrados[:MAXIMO_RESULTADOS]]}
        if len(encontrados) > MAXIMO_RESULTADOS:
            respuesta["mas_resultados"] = (
                f"Hay {len(encontrados)} residentes que encajan; se muestran los "
                f"{MAXIMO_RESULTADOS} que mejor coinciden. Pide un nombre mas concreto.")
        return json.dumps(respuesta, ensure_ascii=False)

    respuesta = {"resultado": "No se encontraron residentes activos con ese nombre"}
    # A quien esta de baja se le nombra, pero sin `id`: asi el asistente puede
    # avisar de que existe y no puede pedir su ficha ni sus datos medicos.
    de_baja, _ = _buscar_por_nombre(
        nombre, Resident.query.filter_by(active=False).all())
    if de_baja:
        respuesta["inactivos"] = [f"{r.name} (consta como dado de baja)"
                                  for r in de_baja[:MAXIMO_RESULTADOS]]
    if parecidos:
        respuesta["sugerencias"] = [r.name for r in parecidos]
    return json.dumps(respuesta, ensure_ascii=False)


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


def _hora(texto: str | None, por_defecto):
    """Convierte "9", "9:30" o "09:30" en un time. `por_defecto` si no se entiende.

    Se acepta la hora suelta porque es como la dice la gente: "entre las nueve y
    las trece" llega al modelo como "9" y "13", no como "09:00".
    """
    if not texto:
        return por_defecto
    m = re.match(r'^\s*(\d{1,2})(?:[:h.](\d{1,2}))?\s*$', str(texto))
    if not m:
        return por_defecto
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return por_defecto
    return dt_time(h, mi)


def _franja(dia, desde_hora, hasta_hora):
    """(inicio, fin, etiqueta) del dia, recortado a la franja si se pide."""
    d = _hora(desde_hora, None)
    h = _hora(hasta_hora, None)
    inicio = datetime.combine(dia, d or datetime.min.time())
    if h:
        fin = datetime.combine(dia, h)
    else:
        fin = datetime.combine(dia + timedelta(days=1), datetime.min.time())
    if fin <= inicio:       # franja al reves o vacia: se ignora y va el dia entero
        return (datetime.combine(dia, datetime.min.time()),
                datetime.combine(dia + timedelta(days=1), datetime.min.time()),
                'todo el día')
    if d or h:
        return inicio, fin, 'de %s a %s' % (inicio.strftime('%H:%M'), fin.strftime('%H:%M'))
    return inicio, fin, 'todo el día'


def _tipo_no_encontrado(tipo: str) -> str:
    return json.dumps({
        "error": f'No hay ningún tipo de atención que coincida con "{tipo}"',
        "tipos_disponibles": [_nombre_tipo(ct) for ct in
                              CareType.query.filter_by(active=True).all()],
    }, ensure_ascii=False)


def _registros_del_tipo(ids, inicio, fin):
    """Registros de esos tipos en la ventana. `care_type_id` es el campo antiguo:
    los registros de antes de los tipos multiples solo lo tienen a el."""
    return CareRecord.query.filter(
        CareRecord.start_time >= inicio, CareRecord.start_time < fin,
        db.or_(CareRecord.care_types.any(CareType.id.in_(ids)),
               CareRecord.care_type_id.in_(ids)),
    ).order_by(CareRecord.start_time).all()


def _residentes_sin_atencion(tipo: str, fecha: str | None = None,
                             desde_hora: str | None = None,
                             hasta_hora: str | None = None) -> str:
    """Quien NO ha recibido esta atencion. La pregunta en negativo.

    El sistema no sabe que residentes necesitan cada atencion: un residente
    continente saldria en la lista de "sin cambio de panal" sin que eso signifique
    nada. Por eso de cada uno se dice cuando la recibio por ultima vez: quien la
    recibe cada dia y hoy no, es un aviso; quien no la ha recibido nunca,
    seguramente no la necesita.
    """
    coincidencias = _tipos_que_coinciden(tipo)
    if not coincidencias:
        return _tipo_no_encontrado(tipo)

    dia = datetime.strptime(fecha, '%Y-%m-%d').date() if fecha else datetime.now().date()
    inicio, fin, etiqueta = _franja(dia, desde_hora, hasta_hora)
    ids = {ct.id for ct in coincidencias}

    recibidas: dict[int, str] = {}
    for c in _registros_del_tipo(ids, inicio, fin):
        if c.resident_id and c.resident_id not in recibidas:
            recibidas[c.resident_id] = c.start_time.strftime('%H:%M')

    # Cuando la recibio por ultima vez cada uno, mirando dos semanas atras. Es lo
    # que separa a quien se ha quedado sin ella de quien no la necesita.
    previas: dict[int, datetime] = {}
    for c in _registros_del_tipo(ids, inicio - timedelta(days=14), inicio):
        if c.resident_id:
            previas[c.resident_id] = c.start_time      # ordenados: gana el ultimo

    sin, con = [], []
    for r in Resident.query.filter_by(active=True).order_by(Resident.name).all():
        if r.id in recibidas:
            con.append({"residente": r.name,
                        "habitacion": r.room_number or 'Sin asignar',
                        "hora": recibidas[r.id]})
            continue
        ultima = previas.get(r.id)
        sin.append({
            "residente": r.name,
            "habitacion": r.room_number or 'Sin asignar',
            "grupo": r.group.name if r.group else None,
            "nivel_dependencia": r.dependency_level,
            "ultima_vez_que_la_recibio": ultima.strftime('%d/%m %H:%M') if ultima else None,
            "horas_desde_la_ultima_vez": (round((inicio - ultima).total_seconds() / 3600)
                                          if ultima else None),
            "la_recibe_habitualmente": ultima is not None,
        })

    # Primero los que si la reciben normalmente y hoy no: son los que importan.
    sin.sort(key=lambda x: (not x["la_recibe_habitualmente"],
                            -(x["horas_desde_la_ultima_vez"] or 0)))

    return json.dumps({
        "tipo_buscado": tipo,
        "tipos_encontrados": [_nombre_tipo(ct) for ct in coincidencias],
        "fecha": dia.strftime('%d/%m/%Y'),
        "franja": etiqueta,
        "total_residentes_activos": len(con) + len(sin),
        "la_han_recibido": con,
        "no_la_han_recibido": sin,
        "como_leerlo": (
            "El sistema NO sabe que residentes necesitan cada atencion, asi que "
            "esta lista no es una lista de fallos. Mirad la_recibe_habitualmente: "
            "quien la tiene en true y hoy no la ha recibido es lo que hay que "
            "revisar, y horas_desde_la_ultima_vez dice cuanto lleva. Quien la "
            "tiene en false no la ha recibido en dos semanas, asi que lo mas "
            "probable es que no la necesite. Nombra primero a los de true."
        ),
    }, ensure_ascii=False)


def _atenciones_por_tipo(tipo: str, fecha: str | None = None, dias: int = 1,
                         desde_hora: str | None = None,
                         hasta_hora: str | None = None) -> str:
    coincidencias = _tipos_que_coinciden(tipo)
    if not coincidencias:
        return _tipo_no_encontrado(tipo)

    dia = datetime.strptime(fecha, '%Y-%m-%d').date() if fecha else datetime.now().date()
    dias = max(1, int(dias or 1))
    if dias == 1:
        inicio, fin, franja = _franja(dia, desde_hora, hasta_hora)
    else:
        # Una franja horaria sobre varios dias no significa nada claro; manda el rango.
        inicio = datetime.combine(dia - timedelta(days=dias - 1), datetime.min.time())
        fin = datetime.combine(dia + timedelta(days=1), datetime.min.time())
        franja = 'todo el día'

    ids = {ct.id for ct in coincidencias}
    records = _registros_del_tipo(ids, inicio, fin)

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
        # Sin esto, una respuesta filtrada de 9 a 13 se contaria como si fuera
        # del dia entero y nadie sabria que se ha mirado solo un rato.
        "franja": franja,
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


def _uso_de_la_aplicacion(dias: int = 30) -> str:
    """Cuanto usa cada trabajadora la webapp, con el contexto para juzgarlo.

    La pregunta que responde ("quien la esta usando menos") es facil de contestar
    mal: quien ha estado de baja o de vacaciones sale a cero sin haber hecho nada
    malo, y quien tiene rol de gestion no usa la webapp del movil en absoluto.
    Por eso se devuelven tambien los dias de turno y los de ausencia, y el
    promedio por dia trabajado, que es lo unico comparable entre personas.
    """
    from datetime import date as _date
    dias = min(max(int(dias or 30), 1), 365)
    desde = datetime.now() - timedelta(days=dias)
    desde_dia = desde.date()
    hoy = _date.today()

    trabajadoras = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    datos = {c.id: {
        "nombre": c.name,
        "rol": c.role,
        "es_admin": bool(c.is_admin),
        "ultima_vez_vista": c.last_active.strftime('%d/%m/%Y %H:%M') if c.last_active else None,
        "dias_desde_la_ultima_vez": (datetime.now() - c.last_active).days if c.last_active else None,
        "entradas_en_la_webapp": 0,
        "limpiezas_registradas": 0,
        "atenciones_registradas": 0,
        "dias_distintos_con_registros": 0,
        "dias_con_turno": 0,
        "dias_de_ausencia": 0,
    } for c in trabajadoras}

    for wid, n in (db.session.query(WorkerDeviceLogin.worker_id, db.func.count())
                   .filter(WorkerDeviceLogin.at >= desde)
                   .group_by(WorkerDeviceLogin.worker_id).all()):
        if wid in datos:
            datos[wid]["entradas_en_la_webapp"] = n

    # Los dias distintos se cuentan en Python: `date(timestamp)` no se escribe
    # igual en SQLite que en PostgreSQL y aqui corren los dos.
    dias_con = {cid: set() for cid in datos}
    for wid, inicio in (db.session.query(CleaningRecord.cleaner_id, CleaningRecord.start_time)
                        .filter(CleaningRecord.start_time >= desde).all()):
        if wid in datos:
            datos[wid]["limpiezas_registradas"] += 1
            if inicio:
                dias_con[wid].add(inicio.date())
    for wid, inicio in (db.session.query(CareRecord.worker_id, CareRecord.start_time)
                        .filter(CareRecord.start_time >= desde).all()):
        if wid in datos:
            datos[wid]["atenciones_registradas"] += 1
            if inicio:
                dias_con[wid].add(inicio.date())
    for wid, fechas in dias_con.items():
        datos[wid]["dias_distintos_con_registros"] = len(fechas)

    for wid, n in (db.session.query(ShiftAssignment.cleaner_id, db.func.count())
                   .filter(ShiftAssignment.date >= desde_dia,
                           ShiftAssignment.date <= hoy)
                   .group_by(ShiftAssignment.cleaner_id).all()):
        if wid in datos:
            datos[wid]["dias_con_turno"] = n

    for aus in Absence.query.filter(Absence.end_date >= desde_dia,
                                    Absence.start_date <= hoy).all():
        if aus.cleaner_id not in datos:
            continue
        ini = max(aus.start_date, desde_dia)
        fin = min(aus.end_date, hoy)
        datos[aus.cleaner_id]["dias_de_ausencia"] += max(0, (fin - ini).days + 1)

    comparables, aparte = [], []
    for d in datos.values():
        registros = d["limpiezas_registradas"] + d["atenciones_registradas"]
        d["registros_totales"] = registros
        # Lo unico comparable entre personas: quien trabaja veinte dias y quien
        # trabaja dos no se pueden medir por el total.
        d["registros_por_dia_con_turno"] = (round(registros / d["dias_con_turno"], 1)
                                            if d["dias_con_turno"] else None)

        # Quien no cuenta, y por que. Se decide aqui y no se deja al criterio del
        # modelo: encabezar la lista es senalar a alguien, y una persona de baja o
        # una gestora que trabaja desde el panel no han hecho nada malo.
        motivo = None
        if d["rol"] == 'gestion' or d["es_admin"]:
            motivo = "Trabaja desde el panel de administracion, no desde la webapp del movil."
        elif not d["dias_con_turno"]:
            motivo = "No ha tenido ningun turno en este periodo."
        elif d["dias_de_ausencia"] >= d["dias_con_turno"] / 2:
            motivo = ("Ha estado ausente %d de los %d dias con turno."
                      % (d["dias_de_ausencia"], d["dias_con_turno"]))
        d["uso_comparable"] = motivo is None
        d["motivo_no_comparable"] = motivo
        (comparables if motivo is None else aparte).append(d)

    comparables.sort(key=lambda d: (d["registros_por_dia_con_turno"] or 0,
                                    d["registros_totales"]))
    aparte.sort(key=lambda d: d["nombre"])

    return json.dumps({
        "periodo_dias": dias,
        "desde": desde.strftime('%d/%m/%Y'),
        "trabajadoras": comparables + aparte,
        "como_leerlo": (
            "Las que tienen uso_comparable=true van primero y ordenadas de menos "
            "uso a mas: la respuesta a 'quien usa menos la aplicacion' es la "
            "primera de esas. Las de uso_comparable=false NO se pueden juzgar y no "
            "hay que nombrarlas como si usaran poco la aplicacion: su "
            "motivo_no_comparable explica por que (de baja, sin turnos, o trabaja "
            "desde el panel). Si hace falta mencionarlas, di el motivo."
        ),
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
    activos = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    encontrados, parecidos = _buscar_por_nombre(nombre, activos)
    if not encontrados:
        respuesta = {"resultado": "No se encontraron trabajadores con ese nombre"}
        if parecidos:
            respuesta["sugerencias"] = [c.name for c in parecidos]
        return json.dumps(respuesta, ensure_ascii=False)
    return json.dumps({"trabajadores": [{
        "id": c.id, "nombre": c.name, "usuario": c.username,
        "es_admin": c.is_admin, "grupos": [g.name for g in c.groups],
    } for c in encontrados[:MAXIMO_RESULTADOS]]}, ensure_ascii=False)


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



def _como_funciona(tema: str) -> str:
    """El procedimiento que se pregunta, en texto.

    El asistente sabia consultar datos pero no explicar como se hacen las
    cosas: a "como se hace una cura?" miraba la base de datos, que no es la
    pregunta.
    """
    texto = GUIAS.get((tema or '').strip().lower())
    if not texto:
        return json.dumps({
            "error": f"No hay guia de '{tema}'.",
            "temas_disponibles": TEMAS,
        }, ensure_ascii=False)
    return json.dumps({"tema": tema, "procedimiento": texto}, ensure_ascii=False)


def _get_tool_handlers(is_admin: bool = False):
    handlers = {
        "como_funciona": lambda args: _como_funciona(args["tema"]),
        "buscar_residente": lambda args: _buscar_residente(args["nombre"]),
        "info_residente": lambda args: _info_residente(args["residente_id"], include_content=is_admin),
        "atenciones_residente": lambda args: _atenciones_residente(args["residente_id"], args.get("fecha")),
        "atenciones_hoy": lambda args: _atenciones_hoy(),
        "atenciones_por_tipo": lambda args: _atenciones_por_tipo(args["tipo"], args.get("fecha"), args.get("dias", 1), args.get("desde_hora"), args.get("hasta_hora")),
        "residentes_sin_atencion": lambda args: _residentes_sin_atencion(args["tipo"], args.get("fecha"), args.get("desde_hora"), args.get("hasta_hora")),
        "tipos_de_atencion": lambda args: _tipos_de_atencion(),
        "limpiezas_hoy": lambda args: _limpiezas_hoy(),
        "ultima_limpieza_zona": lambda args: _ultima_limpieza_zona(args["numero_zona"]),
        "trabajadores_activos": lambda args: _trabajadores_activos(),
        "uso_de_la_aplicacion": lambda args: _uso_de_la_aplicacion(args.get("dias", 30)),
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


# ── El bucle de herramientas ─────────────────────────────────────────────────

MODELO = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048
# Un tope para que una conversacion que se enreda no se lleve la factura ni un
# worker por delante. Al agotarse NO se corta: se hace una ultima llamada sin
# herramientas, que obliga al modelo a responder con lo que ya tiene.
MAX_VUELTAS = 8
# Y un presupuesto de reloj, porque ocho vueltas lentas siguen siendo una
# peticion web colgada. El limite de 10/minuto no protege de esto.
PRESUPUESTO_SEGUNDOS = 90
TIMEOUT_API = 60.0

MAX_TURNOS_HISTORIAL = 10
MAX_CARACTERES_TURNO = 4000


def _crear_cliente(api_key: str):
    """El cliente de Anthropic, con timeout.

    En funcion aparte para que los tests puedan sustituirlo sin tocar la firma
    publica de `chat()` ni parchear el simbolo del SDK. Sin timeout explicito,
    una llamada lenta deja colgado un worker de gunicorn; la transcripcion de
    audio ya pone el suyo.
    """
    return Anthropic(api_key=api_key, timeout=TIMEOUT_API, max_retries=1)


def _normalizar_historial(historial) -> list:
    """Los turnos anteriores que manda el cliente, acotados y saneados.

    Llega del navegador, asi que no se cree nada: se comprueba la forma, se
    recorta el texto y se limita el numero de turnos. Solo viaja el TEXTO
    visible, nunca los bloques de herramienta: esos llevan datos de residentes
    en crudo y no tienen por que bajar al navegador para volver a subir. Ademas,
    mandando solo texto es imposible construir un `tool_use` sin su
    `tool_result`, que la API rechaza con un 400.
    """
    if not isinstance(historial, list):
        return []
    turnos = []
    for entrada in historial[-MAX_TURNOS_HISTORIAL * 2:]:
        if not isinstance(entrada, dict):
            continue
        rol = entrada.get('rol') or entrada.get('role')
        texto = entrada.get('texto') or entrada.get('content') or ''
        if rol not in ('user', 'assistant') or not isinstance(texto, str):
            continue
        texto = texto.strip()[:MAX_CARACTERES_TURNO]
        if not texto:
            continue
        # Dos turnos seguidos del mismo rol se funden: la API los aceptaria,
        # pero el modelo lee peor una conversacion que no alterna.
        if turnos and turnos[-1]['role'] == rol:
            turnos[-1]['content'] += '\n' + texto
        else:
            turnos.append({'role': rol, 'content': texto})
    # Una conversacion empieza por quien pregunta.
    while turnos and turnos[0]['role'] != 'user':
        turnos.pop(0)
    return turnos[-MAX_TURNOS_HISTORIAL:]


def _ejecutar_herramienta(handlers, nombre: str, argumentos) -> tuple:
    """Ejecuta una herramienta. Devuelve (contenido, es_error). Nunca lanza.

    Antes no habia guardia ninguna: si el modelo omitia un parametro o se
    inventaba una fecha, la excepcion subia hasta el blueprint y la persona veia
    "Error del chatbot" sin mas. El modelo nunca llegaba a ver el fallo, asi que
    tampoco podia corregirse. Ahora el error vuelve como resultado de la
    herramienta y el modelo reintenta con los parametros bien.
    """
    handler = handlers.get(nombre)
    if handler is None:
        return json.dumps({
            "error": f"La herramienta '{nombre}' no esta disponible.",
            "disponibles": sorted(handlers),
        }, ensure_ascii=False), True
    try:
        return handler(argumentos if isinstance(argumentos, dict) else {}), False
    except KeyError as e:
        falta = str(e).strip("'")
        return json.dumps({
            "error": f"Falta el parametro obligatorio '{falta}'.",
        }, ensure_ascii=False), True
    except ValueError:
        return json.dumps({
            "error": "Algun valor no es valido. Las fechas van en formato AAAA-MM-DD.",
        }, ensure_ascii=False), True
    except Exception as e:
        # Una consulta que peta deja la sesion de SQLAlchemy inservible, y sin
        # esto las herramientas siguientes de la misma pregunta fallarian todas
        # en cascada por un fallo que ya paso.
        db.session.rollback()
        # Nunca los argumentos: llevan nombres de residentes.
        app.logger.error('La herramienta %s ha fallado: %s', nombre, e)
        return json.dumps({
            "error": "No se ha podido consultar ese dato.",
        }, ensure_ascii=False), True


def _texto_de(respuesta) -> str:
    partes = [b.text for b in respuesta.content
              if getattr(b, 'type', None) == 'text' and getattr(b, 'text', '')]
    return '\n'.join(partes)


def chat(message: str, api_key: str, is_admin: bool = False,
         historial=None) -> str:
    """Procesa un mensaje del usuario y devuelve la respuesta del chatbot.

    `historial` son los turnos anteriores de la conversacion, que manda el
    cliente. Sin el, cada pregunta llegaba suelta y "y de ella?" no podia
    funcionar.
    """
    client = _crear_cliente(api_key)
    handlers = _get_tool_handlers(is_admin)
    # Se lee una sola vez: todas las vueltas tienen que ir con el mismo prompt,
    # y no tiene sentido volver a la BD en cada una.
    prompt = system_prompt()
    # La fecha va aparte del prompt, en el turno del usuario: metida dentro del
    # prompt cambiaria sus bytes cada dia (y cada minuto, con la hora), que es
    # justo lo que impide cachearlo.
    contexto = _bloque_de_fecha()
    available_tools = [t for t in TOOLS if t['name'] in handlers]

    messages = _normalizar_historial(historial)
    messages.append({"role": "user", "content": f'{contexto}\n\n{message}'})

    empezado = datetime.now()
    respuesta = None
    for vuelta in range(MAX_VUELTAS):
        sin_tiempo = (datetime.now() - empezado).total_seconds() > PRESUPUESTO_SEGUNDOS
        respuesta = client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            system=prompt,
            messages=messages,
            **({} if sin_tiempo else {"tools": available_tools}),
        )
        if sin_tiempo or respuesta.stop_reason != "tool_use":
            break

        resultados = []
        for bloque in respuesta.content:
            if getattr(bloque, 'type', None) != "tool_use":
                continue
            contenido, es_error = _ejecutar_herramienta(handlers, bloque.name, bloque.input)
            # Un resultado por cada bloque, fallos incluidos y todos en el mismo
            # mensaje: dejarse uno es un 400, y repartirlos en varios mensajes
            # ensena al modelo a dejar de pedir herramientas en paralelo.
            resultados.append({
                "type": "tool_result",
                "tool_use_id": bloque.id,
                "content": contenido,
                **({"is_error": True} if es_error else {}),
            })

        messages.append({"role": "assistant", "content": respuesta.content})
        messages.append({"role": "user", "content": resultados})
    else:
        # Agotadas las vueltas: una ultima sin herramientas para que responda
        # con lo que tenga, en vez de devolver un error despues de todo el
        # trabajo hecho.
        respuesta = client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            system=prompt,
            messages=messages,
        )

    if getattr(respuesta, 'stop_reason', None) == 'refusal':
        return ('No puedo responder a eso. Si crees que es un error, '
                'prueba a preguntarlo de otra forma.')
    return _texto_de(respuesta) or "No he podido generar una respuesta."

