"""Clinical assessment scales (Barthel, Norton) and weight loss detection."""
from __future__ import annotations
import json as _json
from datetime import datetime, timedelta, date

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_login import current_user
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import joinedload

from .. import app, db
from ..models import (
    Resident, Cleaner, AssessmentRecord, Notification,
    VitalSignReading, VitalSignType, CareRecord, CleaningRecord,
    Incident, Room,
)
from ..utils import admin_required

bp = Blueprint('assessments', __name__)


# ── Scale definitions ─────────────────────────────────────────────────────────

BARTHEL_ITEMS = [
    {'key': 'comer', 'label': 'Comer', 'options': [
        (0, 'Incapaz'), (5, 'Necesita ayuda'), (10, 'Independiente')]},
    {'key': 'trasladarse', 'label': 'Trasladarse silla-cama', 'options': [
        (0, 'Incapaz, no se mantiene sentado'),
        (5, 'Gran ayuda (1-2 personas, puede sentarse)'),
        (10, 'Minima ayuda (verbal o fisica)'),
        (15, 'Independiente')]},
    {'key': 'aseo', 'label': 'Aseo personal', 'options': [
        (0, 'Dependiente'), (5, 'Independiente (cara, pelo, dientes, afeitarse)')]},
    {'key': 'wc', 'label': 'Uso del WC', 'options': [
        (0, 'Dependiente'), (5, 'Necesita ayuda parcial'), (10, 'Independiente')]},
    {'key': 'banarse', 'label': 'Banarse o ducharse', 'options': [
        (0, 'Dependiente'), (5, 'Independiente')]},
    {'key': 'desplazarse', 'label': 'Desplazarse', 'options': [
        (0, 'Inmovil o < 50 metros'), (5, 'Independiente en silla de ruedas > 50m'),
        (10, 'Camina con ayuda (verbal o fisica) > 50m'),
        (15, 'Independiente > 50 metros')]},
    {'key': 'escaleras', 'label': 'Subir y bajar escaleras', 'options': [
        (0, 'Incapaz'), (5, 'Necesita ayuda (verbal, fisica, ortesis)'),
        (10, 'Independiente')]},
    {'key': 'vestirse', 'label': 'Vestirse y desvestirse', 'options': [
        (0, 'Dependiente'), (5, 'Necesita ayuda (se viste al menos la mitad)'),
        (10, 'Independiente (incluye botones, cremalleras, cordones)')]},
    {'key': 'heces', 'label': 'Control de heces', 'options': [
        (0, 'Incontinente (o necesita enema)'), (5, 'Accidente ocasional (< 1/semana)'),
        (10, 'Continente')]},
    {'key': 'orina', 'label': 'Control de orina', 'options': [
        (0, 'Incontinente o sondado'), (5, 'Accidente ocasional (< 1/dia)'),
        (10, 'Continente (>= 7 dias)')]},
]

BARTHEL_INTERPRET = [
    (0, 20, 'total', 'Dependencia total'),
    (21, 60, 'severa', 'Dependencia severa'),
    (61, 90, 'moderada', 'Dependencia moderada'),
    (91, 99, 'leve', 'Dependencia leve'),
    (100, 100, 'independiente', 'Independiente'),
]

NORTON_ITEMS = [
    {'key': 'estado_fisico', 'label': 'Estado fisico general', 'options': [
        (1, 'Muy malo'), (2, 'Malo'), (3, 'Regular'), (4, 'Bueno')]},
    {'key': 'estado_mental', 'label': 'Estado mental', 'options': [
        (1, 'Estuporoso / comatoso'), (2, 'Confuso'), (3, 'Apatico'), (4, 'Alerta')]},
    {'key': 'actividad', 'label': 'Actividad', 'options': [
        (1, 'Encamado'), (2, 'Sentado'), (3, 'Camina con ayuda'), (4, 'Ambulante')]},
    {'key': 'movilidad', 'label': 'Movilidad', 'options': [
        (1, 'Inmovil'), (2, 'Muy limitada'), (3, 'Disminuida'), (4, 'Total')]},
    {'key': 'incontinencia', 'label': 'Incontinencia', 'options': [
        (1, 'Urinaria y fecal'), (2, 'Urinaria habitual'), (3, 'Ocasional'), (4, 'Ninguna')]},
]

NORTON_INTERPRET = [
    (5, 9, 'muy_alto', 'Riesgo muy alto'),
    (10, 12, 'alto', 'Riesgo alto'),
    (13, 14, 'medio', 'Riesgo medio'),
    (15, 20, 'minimo', 'Riesgo minimo / sin riesgo'),
]

DEPENDENCY_MAP = {
    'total': 'total', 'severa': 'severe', 'moderada': 'moderate',
    'leve': 'mild', 'independiente': 'autonomous',
}



# ── Helpers ───────────────────────────────────────────────────────────────────

def _interpret(score, table):
    for lo, hi, key, label in table:
        if lo <= score <= hi:
            return key, label
    return 'desconocido', 'Desconocido'


# ── Assessment list ───────────────────────────────────────────────────────────

@bp.route('/admin/assessments')
@admin_required
def admin_assessments():
    """List all clinical assessments with filters."""
    resident_id = request.args.get('resident_id', type=int)
    scale_type = request.args.get('scale_type', '')
    page = request.args.get('page', 1, type=int)

    query = AssessmentRecord.query.options(
        joinedload(AssessmentRecord.resident),
        joinedload(AssessmentRecord.assessor),
    )
    if resident_id:
        query = query.filter(AssessmentRecord.resident_id == resident_id)
    if scale_type:
        query = query.filter(AssessmentRecord.scale_type == scale_type)

    query = query.order_by(AssessmentRecord.assessed_at.desc())
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()

    # Find residents without recent assessments (>6 months)
    cutoff = datetime.now() - timedelta(days=180)
    all_active_ids = {r.id for r in residents}
    recent_ids = {r[0] for r in db.session.query(AssessmentRecord.resident_id).filter(
        AssessmentRecord.assessed_at >= cutoff,
    ).distinct().all()}
    stale_residents = [r for r in residents if r.id not in recent_ids]

    return render_template('admin_assessments.html',
        assessments=pagination.items, pagination=pagination,
        residents=residents, stale_residents=stale_residents,
        filters={'resident_id': resident_id, 'scale_type': scale_type},
    )


# ── Barthel form ──────────────────────────────────────────────────────────────

@bp.route('/admin/assessments/barthel', methods=['GET', 'POST'])
@admin_required
def assessment_barthel():
    resident_id = request.args.get('resident_id', type=int) or (
        request.form.get('resident_id', type=int) if request.method == 'POST' else None)

    if request.method == 'POST':
        if not resident_id:
            flash('Selecciona un residente.', 'danger')
            return redirect(url_for('assessments.admin_assessments'))

        answers = {}
        total = 0
        for item in BARTHEL_ITEMS:
            val = request.form.get(item['key'], type=int)
            if val is None:
                flash(f'Falta respuesta: {item["label"]}', 'danger')
                return redirect(request.url)
            answers[item['key']] = val
            total += val

        interp_key, interp_label = _interpret(total, BARTHEL_INTERPRET)

        record = AssessmentRecord(
            resident_id=resident_id, scale_type='barthel',
            score=total, interpretation=interp_key,
            answers_json=_json.dumps(answers, ensure_ascii=False),
            notes=request.form.get('notes', '').strip() or None,
            assessed_by=current_user.id,
        )
        db.session.add(record)

        # Update resident dependency_level
        resident = db.session.get(Resident, resident_id)
        if resident and interp_key in DEPENDENCY_MAP:
            resident.dependency_level = DEPENDENCY_MAP[interp_key]

        db.session.commit()
        flash(f'Barthel guardado: {total}/100 — {interp_label}', 'success')
        return redirect(url_for('residents.resident_detail', resident_id=resident_id))

    # GET
    resident = db.session.get(Resident, resident_id) if resident_id else None
    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()
    return render_template('admin_assessment_barthel.html',
        resident=resident, residents=residents, items=BARTHEL_ITEMS,
        interpret_table=BARTHEL_INTERPRET,
    )


# ── Norton form ───────────────────────────────────────────────────────────────

@bp.route('/admin/assessments/norton', methods=['GET', 'POST'])
@admin_required
def assessment_norton():
    resident_id = request.args.get('resident_id', type=int) or (
        request.form.get('resident_id', type=int) if request.method == 'POST' else None)

    if request.method == 'POST':
        if not resident_id:
            flash('Selecciona un residente.', 'danger')
            return redirect(url_for('assessments.admin_assessments'))

        answers = {}
        total = 0
        for item in NORTON_ITEMS:
            val = request.form.get(item['key'], type=int)
            if val is None:
                flash(f'Falta respuesta: {item["label"]}', 'danger')
                return redirect(request.url)
            answers[item['key']] = val
            total += val

        interp_key, interp_label = _interpret(total, NORTON_INTERPRET)

        record = AssessmentRecord(
            resident_id=resident_id, scale_type='norton',
            score=total, interpretation=interp_key,
            answers_json=_json.dumps(answers, ensure_ascii=False),
            notes=request.form.get('notes', '').strip() or None,
            assessed_by=current_user.id,
        )
        db.session.add(record)

        # Generate notification if high risk
        if total <= 12:
            resident = db.session.get(Resident, resident_id)
            resident_name = resident.name if resident else 'Residente'
            db.session.add(Notification(
                type='norton_alert',
                title=f'Norton {interp_label}: {resident_name} ({total}/20)',
                severity='warning' if total >= 10 else 'critical',
                resident_id=resident_id,
                link=f'/admin/resident/{resident_id}',
            ))

        db.session.commit()
        flash(f'Norton guardado: {total}/20 — {interp_label}', 'success')
        return redirect(url_for('residents.resident_detail', resident_id=resident_id))

    # GET
    resident = db.session.get(Resident, resident_id) if resident_id else None
    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()
    return render_template('admin_assessment_norton.html',
        resident=resident, residents=residents, items=NORTON_ITEMS,
        interpret_table=NORTON_INTERPRET,
    )


# ── Weight loss detection (uses existing VitalSignReading for "Peso") ─────────

def check_weight_loss_from_vitals(resident_id: int, current_weight: float):
    """Check for significant weight loss using vital sign readings named 'Peso'."""
    resident = db.session.get(Resident, resident_id)
    if not resident:
        return
    resident_name = resident.name
    now = datetime.now()

    # Find all weight readings for this resident via VitalSignType named like 'peso'
    weight_readings = db.session.query(VitalSignReading.value, CareRecord.start_time).join(
        CareRecord, VitalSignReading.care_record_id == CareRecord.id
    ).join(
        VitalSignType, VitalSignReading.vital_sign_type_id == VitalSignType.id
    ).filter(
        CareRecord.resident_id == resident_id,
        VitalSignType.name.ilike('%peso%'),
        CareRecord.start_time.isnot(None),
    ).order_by(CareRecord.start_time.asc()).all()

    if len(weight_readings) < 2:
        return

    # Check 30-day loss (>5%)
    cutoff_30 = now - timedelta(days=30)
    readings_30 = [r for r in weight_readings if r.start_time >= cutoff_30]
    if readings_30 and readings_30[0].value > 0:
        first_val = readings_30[0].value
        loss_pct = ((first_val - current_weight) / first_val) * 100
        if loss_pct >= 5:
            title = f'Perdida de peso: {resident_name} ha perdido {loss_pct:.1f}% en 30 dias ({first_val:.1f} -> {current_weight:.1f} kg)'
            from .notifications import _notif_exists
            if not _notif_exists('weight_alert', title, hours=48):
                db.session.add(Notification(
                    type='weight_alert', title=title, severity='warning',
                    resident_id=resident_id, link=f'/admin/resident/{resident_id}',
                ))
            return

    # Check 180-day loss (>10%)
    cutoff_180 = now - timedelta(days=180)
    readings_180 = [r for r in weight_readings if r.start_time >= cutoff_180]
    if readings_180 and readings_180[0].value > 0:
        first_val = readings_180[0].value
        loss_pct = ((first_val - current_weight) / first_val) * 100
        if loss_pct >= 10:
            title = f'Perdida de peso grave: {resident_name} ha perdido {loss_pct:.1f}% en 6 meses ({first_val:.1f} -> {current_weight:.1f} kg)'
            from .notifications import _notif_exists
            if not _notif_exists('weight_alert', title, hours=48):
                db.session.add(Notification(
                    type='weight_alert', title=title, severity='critical',
                    resident_id=resident_id, link=f'/admin/resident/{resident_id}',
                ))


# ── AI helpers ────────────────────────────────────────────────────────────────

def _call_claude(system_prompt: str, user_message: str) -> str:
    """Call Claude API with a simple prompt (no tools)."""
    api_key = app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return 'Error: API key de Anthropic no configurada.'
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=1500,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_message}],
    )
    parts = [b.text for b in response.content if hasattr(b, 'text')]
    return '\n'.join(parts) or 'No se ha podido generar una respuesta.'


def _build_resident_context(resident_id: int, days: int = 7) -> dict:
    """Build structured data for AI analysis of a resident."""
    resident = db.session.get(Resident, resident_id)
    if not resident:
        return {}

    cutoff = datetime.now() - timedelta(days=days)

    # Profile
    dep_labels = {'autonomous': 'Autonomo', 'mild': 'Leve', 'moderate': 'Moderado',
                  'severe': 'Severo', 'total': 'Total'}
    profile = {
        'name': resident.name,
        'room': resident.room_number or '—',
        'diagnoses': resident.diagnoses or '',
        'allergies': resident.allergies or '',
        'medication': resident.current_medication or '',
        'dependency': dep_labels.get(resident.dependency_level, resident.dependency_level or '—'),
        'relevant_info': resident.relevant_info or '',
    }

    # Care records
    care_records = CareRecord.query.options(
        joinedload(CareRecord.worker), joinedload(CareRecord.care_types),
        joinedload(CareRecord.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
    ).filter(
        CareRecord.resident_id == resident_id,
        CareRecord.start_time >= cutoff,
        CareRecord.end_time.isnot(None),
    ).order_by(CareRecord.start_time.desc()).limit(50).all()

    care_data = []
    worker_notes = []
    vital_data = []
    for cr in care_records:
        types_str = ', '.join(ct.name for ct in cr.care_types) if cr.care_types else '—'
        dur = cr.calculate_duration()
        care_data.append({
            'date': cr.start_time.strftime('%d/%m/%Y %H:%M'),
            'types': types_str,
            'worker': cr.worker.name if cr.worker else '—',
            'duration_min': round(dur / 60, 1) if dur else None,
        })
        if cr.notes:
            worker_notes.append(f"{cr.start_time.strftime('%d/%m')}: {cr.notes}")
        for reading in cr.vital_sign_readings:
            vst = reading.vital_sign_type
            vital_data.append({
                'date': cr.start_time.strftime('%d/%m/%Y'),
                'type': vst.name if vst else '—',
                'value': reading.value,
                'unit': vst.unit if vst else '',
            })

    # Assessments
    assessments = AssessmentRecord.query.filter(
        AssessmentRecord.resident_id == resident_id,
        AssessmentRecord.assessed_at >= cutoff,
    ).order_by(AssessmentRecord.assessed_at.desc()).all()
    assess_data = [{'date': a.assessed_at.strftime('%d/%m/%Y'), 'scale': a.scale_type,
                     'score': a.score, 'interpretation': a.interpretation} for a in assessments]

    # Also include latest assessment even if older than cutoff
    for scale in ['barthel', 'norton']:
        if not any(a['scale'] == scale for a in assess_data):
            latest = AssessmentRecord.query.filter_by(
                resident_id=resident_id, scale_type=scale,
            ).order_by(AssessmentRecord.assessed_at.desc()).first()
            if latest:
                assess_data.append({'date': latest.assessed_at.strftime('%d/%m/%Y'),
                                    'scale': latest.scale_type, 'score': latest.score,
                                    'interpretation': latest.interpretation})

    # Incidents
    incidents = Incident.query.filter(
        Incident.resident_id == resident_id,
        Incident.created_at >= cutoff,
    ).order_by(Incident.created_at.desc()).all()
    incident_data = [{'date': i.created_at.strftime('%d/%m/%Y'), 'title': i.title,
                       'severity': i.severity, 'status': i.status} for i in incidents]

    # Cleaning
    room = Room.query.filter_by(number=resident.room_number).first() if resident.room_number else None
    cleaning_data = []
    if room:
        cleanings = CleaningRecord.query.filter(
            CleaningRecord.room_id == room.id,
            CleaningRecord.start_time >= cutoff,
            CleaningRecord.end_time.isnot(None),
        ).order_by(CleaningRecord.start_time.desc()).limit(20).all()
        for cl in cleanings:
            dur = cl.calculate_duration()
            cleaning_data.append({
                'date': cl.start_time.strftime('%d/%m/%Y'),
                'duration_min': round(dur / 60, 1) if dur else None,
            })
            if cl.notes:
                worker_notes.append(f"{cl.start_time.strftime('%d/%m')} (limpieza): {cl.notes}")

    return {
        'profile': profile,
        'care_records': care_data,
        'vital_signs': vital_data,
        'assessments': assess_data,
        'incidents': incident_data,
        'cleaning': cleaning_data,
        'worker_notes': worker_notes,
    }


def _context_to_text(ctx: dict, days: int) -> str:
    """Format context dict as readable text for Claude."""
    lines = [f"RESIDENTE: {ctx['profile']['name']} | Hab. {ctx['profile']['room']}"]
    p = ctx['profile']
    if p['diagnoses']:
        lines.append(f"Diagnosticos: {p['diagnoses']}")
    if p['allergies']:
        lines.append(f"Alergias: {p['allergies']}")
    if p['medication']:
        lines.append(f"Medicacion: {p['medication']}")
    lines.append(f"Nivel de dependencia: {p['dependency']}")
    if p['relevant_info']:
        lines.append(f"Info relevante: {p['relevant_info']}")

    if ctx['assessments']:
        lines.append(f"\nVALORACIONES:")
        for a in ctx['assessments']:
            lines.append(f"  {a['date']} - {a['scale'].capitalize()}: {a['score']} ({a['interpretation']})")

    if ctx['care_records']:
        lines.append(f"\nATENCIONES ({len(ctx['care_records'])} en los ultimos {days} dias):")
        for cr in ctx['care_records'][:20]:
            dur = f", {cr['duration_min']}min" if cr['duration_min'] else ''
            lines.append(f"  {cr['date']} - {cr['types']} por {cr['worker']}{dur}")

    if ctx['vital_signs']:
        lines.append(f"\nCONSTANTES VITALES:")
        for vs in ctx['vital_signs'][:20]:
            lines.append(f"  {vs['date']} - {vs['type']}: {vs['value']} {vs['unit']}")

    if ctx['incidents']:
        lines.append(f"\nINCIDENCIAS:")
        for inc in ctx['incidents']:
            lines.append(f"  {inc['date']} - {inc['title']} (sev: {inc['severity']}, estado: {inc['status']})")

    if ctx['cleaning']:
        lines.append(f"\nLIMPIEZAS HABITACION ({len(ctx['cleaning'])}):")
        for cl in ctx['cleaning'][:10]:
            dur = f" ({cl['duration_min']}min)" if cl['duration_min'] else ''
            lines.append(f"  {cl['date']}{dur}")

    if ctx['worker_notes']:
        lines.append(f"\nNOTAS DEL PERSONAL:")
        for note in ctx['worker_notes'][:15]:
            lines.append(f"  - {note}")

    return '\n'.join(lines)


# ── AI summary endpoint ──────────────────────────────────────────────────────

SUMMARY_SYSTEM = """Eres un asistente clinico de la residencia de ancianos La Vila Gran.
Genera informes clinicos estructurados en HTML sobre residentes basandote en los datos proporcionados.
Escribe siempre en espanol. Usa un tono profesional sanitario.
No inventes datos — solo comenta lo que aparece en los datos.
Si no hay datos suficientes, indicalo brevemente.

FORMATO: Devuelve SOLO el contenido HTML del informe (sin <html>, <head> ni <body>).
Usa estas secciones con encabezados h3:
- Situacion general (parrafo breve del estado actual)
- Atenciones recibidas (resumen de cuidados, frecuencia, tipos)
- Constantes vitales (valores recientes, tendencias si las hay)
- Valoracion funcional (Barthel, Norton si hay datos)
- Observaciones del personal (notas relevantes de los trabajadores)
- Incidencias (si las hay)
- Aspectos a vigilar / recomendaciones (si detectas algo que requiera atencion)

Usa <strong> para datos importantes, <ul>/<li> para listas.
Si una seccion no tiene datos, omitela. No uses emojis."""

@bp.route('/api/resident/<int:resident_id>/ai-summary', methods=['POST'])
@admin_required
def ai_summary(resident_id: int):
    """Generate AI clinical report for a resident."""
    data = request.get_json() or {}
    period = data.get('period', 'week')
    days_map = {'today': 1, 'week': 7, 'month': 30}
    days = days_map.get(period, 7)

    ctx = _build_resident_context(resident_id, days)
    if not ctx:
        return jsonify({'error': 'Residente no encontrado'}), 404

    resident = db.session.get(Resident, resident_id)
    context_text = _context_to_text(ctx, days)
    period_label = {'today': 'de hoy', 'week': 'de la ultima semana', 'month': 'del ultimo mes'}
    prompt = f"""Genera un informe clinico {period_label.get(period, 'reciente')} de este residente.

{context_text}"""

    try:
        summary_html = _call_claude(SUMMARY_SYSTEM, prompt)
        # Strip markdown code fences if Claude wraps the HTML in them
        summary_html = summary_html.strip()
        if summary_html.startswith('```'):
            summary_html = summary_html.split('\n', 1)[1] if '\n' in summary_html else summary_html[3:]
        if summary_html.endswith('```'):
            summary_html = summary_html.rsplit('```', 1)[0]
        summary_html = summary_html.strip()
    except Exception as e:
        return jsonify({'error': f'Error al generar informe: {str(e)}'}), 500

    # Build full report metadata
    period_titles = {'today': 'Informe del dia', 'week': 'Informe semanal', 'month': 'Informe mensual'}
    report = {
        'html': summary_html,
        'period': period,
        'title': period_titles.get(period, 'Informe'),
        'resident_name': resident.name if resident else '',
        'resident_room': resident.room_number or '' if resident else '',
        'generated_at': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'generated_by': current_user.name if current_user else '',
    }
    return jsonify(report)


# ── Assessment history data for resident detail ──────────────────────────────

def get_resident_assessment_data(resident_id: int) -> dict:
    """Fetch assessment data for the resident detail page."""
    assessments = AssessmentRecord.query.filter_by(
        resident_id=resident_id,
    ).order_by(AssessmentRecord.assessed_at.desc()).all()

    barthel_records = [a for a in assessments if a.scale_type == 'barthel']
    norton_records = [a for a in assessments if a.scale_type == 'norton']

    return {
        'barthel_records': barthel_records,
        'norton_records': norton_records,
    }
