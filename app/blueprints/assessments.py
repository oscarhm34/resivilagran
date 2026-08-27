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
    Incident, Room, Activity, ActivityParticipation,
    CarePlan, DailyDigest, FallRecord,
)
from ..utils import admin_required, _safe_commit, _safe_flush, log_audit

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

PFEIFFER_QUESTIONS = [
    {'key': 'q1', 'question': '¿Qué día de la semana es hoy?'},
    {'key': 'q2', 'question': '¿Qué día del mes es hoy?'},
    {'key': 'q3', 'question': '¿En qué mes estamos?'},
    {'key': 'q4', 'question': '¿En qué año estamos?'},
    {'key': 'q5', 'question': '¿En qué lugar estamos?'},
    {'key': 'q6', 'question': '¿Cuál es su número de teléfono? (o dirección si no tiene)'},
    {'key': 'q7', 'question': '¿Cuántos años tiene?'},
    {'key': 'q8', 'question': '¿Dónde nació?'},
    {'key': 'q9', 'question': '¿Cómo se llama el presidente del gobierno?'},
    {'key': 'q10', 'question': '¿Puede restar de 3 en 3 desde 20? (20, 17, 14...)'},
]

PFEIFFER_INTERPRET = [
    (0, 2, 'normal', 'Funcionamiento cognitivo normal'),
    (3, 4, 'leve', 'Deterioro cognitivo leve'),
    (5, 7, 'moderado', 'Deterioro cognitivo moderado'),
    (8, 10, 'severo', 'Deterioro cognitivo severo'),
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

        ok, error = _safe_flush('Error al guardar la valoracion Barthel')
        if not ok:
            flash(error, 'danger')
            return redirect(request.url)
        log_audit('create', 'assessment_record', record.id,
                  {'escala': 'barthel', 'resident_id': resident_id})
        ok, error = _safe_commit('Error al guardar la valoracion Barthel')
        if not ok:
            flash(error, 'danger')
            return redirect(request.url)

        # Auto-review PAI if score indicates decline
        try:
            prev = AssessmentRecord.query.filter(
                AssessmentRecord.resident_id == resident_id,
                AssessmentRecord.scale_type == 'barthel',
                AssessmentRecord.id != record.id,
            ).order_by(AssessmentRecord.assessed_at.desc()).first()
            if prev and total < prev.score - 5:
                auto_review_care_plan(resident_id,
                    f'Barthel ha bajado de {prev.score} a {total}/100 ({interp_label})')
        except Exception:
            pass

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

        ok, error = _safe_flush('Error al guardar la valoracion Norton')
        if not ok:
            flash(error, 'danger')
            return redirect(request.url)
        log_audit('create', 'assessment_record', record.id,
                  {'escala': 'norton', 'resident_id': resident_id})
        ok, error = _safe_commit('Error al guardar la valoracion Norton')
        if not ok:
            flash(error, 'danger')
            return redirect(request.url)

        # Auto-review PAI if Norton worsened
        try:
            prev = AssessmentRecord.query.filter(
                AssessmentRecord.resident_id == resident_id,
                AssessmentRecord.scale_type == 'norton',
                AssessmentRecord.id != record.id,
            ).order_by(AssessmentRecord.assessed_at.desc()).first()
            if prev and total < prev.score - 1:
                auto_review_care_plan(resident_id,
                    f'Norton ha bajado de {prev.score} a {total}/20 ({interp_label}). Riesgo UPP aumentado.')
        except Exception:
            pass

        flash(f'Norton guardado: {total}/20 — {interp_label}', 'success')
        return redirect(url_for('residents.resident_detail', resident_id=resident_id))

    # GET
    resident = db.session.get(Resident, resident_id) if resident_id else None
    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()
    return render_template('admin_assessment_norton.html',
        resident=resident, residents=residents, items=NORTON_ITEMS,
        interpret_table=NORTON_INTERPRET,
    )


# ── Pfeiffer (SPMSQ) ────────────────────────────────────────────────────────

@bp.route('/admin/assessments/pfeiffer', methods=['GET', 'POST'])
@admin_required
def admin_assessment_pfeiffer():
    resident_id = request.args.get('resident_id', type=int) or request.form.get('resident_id', type=int)

    if request.method == 'POST':
        if not resident_id:
            flash('Selecciona un residente.', 'danger')
            return redirect(url_for('assessments.admin_assessments'))

        answers = {}
        errors = 0
        for q in PFEIFFER_QUESTIONS:
            val = request.form.get(q['key'])
            # 1 = error, 0 = correct
            is_error = 1 if val == '1' else 0
            answers[q['key']] = is_error
            errors += is_error

        interp_key, interp_label = _interpret(errors, PFEIFFER_INTERPRET)

        record = AssessmentRecord(
            resident_id=resident_id, scale_type='pfeiffer',
            score=errors, interpretation=interp_key,
            answers_json=_json.dumps(answers, ensure_ascii=False),
            notes=request.form.get('notes', '').strip() or None,
            assessed_by=current_user.id,
        )
        db.session.add(record)
        ok, error = _safe_flush('Error al guardar la valoracion Pfeiffer')
        if not ok:
            flash(error, 'danger')
            return redirect(request.url)
        log_audit('create', 'assessment_record', record.id,
                  {'escala': 'pfeiffer', 'resident_id': resident_id})
        ok, error = _safe_commit('Error al guardar la valoracion Pfeiffer')
        if not ok:
            flash(error, 'danger')
            return redirect(request.url)
        flash(f'Pfeiffer guardado: {errors}/10 errores — {interp_label}', 'success')
        return redirect(url_for('residents.resident_detail', resident_id=resident_id))

    resident = db.session.get(Resident, resident_id) if resident_id else None
    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()
    return render_template('admin_assessment_pfeiffer.html',
        resident=resident, residents=residents, questions=PFEIFFER_QUESTIONS,
        interpret_table=PFEIFFER_INTERPRET,
    )


# ── AI suggestions for assessments ────────────────────────────────────────────

@bp.route('/api/resident/<int:resident_id>/ai-suggest-assessment', methods=['POST'])
@admin_required
def ai_suggest_assessment(resident_id: int):
    """Use AI to suggest Barthel or Norton answers based on recent worker notes."""
    data = request.get_json() or {}
    scale = data.get('scale', 'barthel')

    ctx = _build_resident_context(resident_id, days=30)
    if not ctx:
        return jsonify({'error': 'Residente no encontrado'}), 404

    context_text = _context_to_text(ctx, 30)

    if scale == 'barthel':
        items_desc = '\n'.join(
            f"- {item['key']}: {item['label']} (opciones: {', '.join(f'{v}={l}' for v, l in item['options'])})"
            for item in BARTHEL_ITEMS
        )
        system = f"""Analiza los datos del residente y sugiere valores para cada item del Indice de Barthel.
Basate SOLO en las observaciones del personal, atenciones registradas y nivel de dependencia actual.
Si no hay informacion suficiente para un item, elige el valor intermedio.

Items del Barthel:
{items_desc}

Responde SOLO con un JSON object con las claves de cada item y el valor numerico sugerido.
Ejemplo: {{"comer": 5, "trasladarse": 10, "aseo": 0, ...}}"""
    else:
        items_desc = '\n'.join(
            f"- {item['key']}: {item['label']} (opciones: {', '.join(f'{v}={l}' for v, l in item['options'])})"
            for item in NORTON_ITEMS
        )
        system = f"""Analiza los datos del residente y sugiere valores para cada item de la Escala de Norton.
Basate SOLO en las observaciones del personal, atenciones registradas y nivel de dependencia actual.
Si no hay informacion suficiente para un item, elige el valor intermedio.

Items del Norton:
{items_desc}

Responde SOLO con un JSON object con las claves de cada item y el valor numerico sugerido.
Ejemplo: {{"estado_fisico": 3, "estado_mental": 4, ...}}"""

    prompt = f"Datos del residente:\n\n{context_text}"

    try:
        response = _call_claude(system, prompt)
        response = response.strip()
        if response.startswith('```'):
            response = response.split('\n', 1)[1] if '\n' in response else response[3:]
        if response.endswith('```'):
            response = response.rsplit('```', 1)[0]
        suggestions = _json.loads(response.strip())
    except Exception:
        return jsonify({'error': 'No se han podido generar sugerencias'}), 500

    return jsonify({'suggestions': suggestions})


# ── Inspection reports ────────────────────────────────────────────────────────

INSPECTION_SYSTEM = """Eres un profesional sanitario redactando documentos oficiales para la residencia de ancianos La Vila Gran.
Genera documentos formales en HTML estructurado, con lenguaje tecnico-sanitario apropiado para inspecciones
y auditorias de la Conselleria de Sanitat.
Escribe siempre en espanol. No inventes datos. Usa solo la informacion proporcionada.

FORMATO: Devuelve SOLO contenido HTML (sin <html>, <head> ni <body>).
Usa h3 para secciones, <strong> para datos clave, <ul>/<li> para listados, <table> cuando proceda.
No uses emojis."""

REPORT_TYPES = {
    'monthly': {
        'title': 'Informe mensual de seguimiento',
        'prompt': """Genera un informe mensual de seguimiento del residente para la inspeccion.
Secciones:
- Datos del residente (nombre, habitacion, diagnosticos, alergias, nivel de dependencia)
- Estado funcional (ultima valoracion Barthel y Norton con interpretacion)
- Evolucion asistencial (resumen de atenciones del mes: tipos, frecuencia, observaciones relevantes)
- Constantes vitales (resumen de lecturas del mes, valores medios, alertas si las hubo)
- Incidencias (resumen de incidencias del mes si las hubo)
- Plan de cuidados actual (basado en los tipos de atencion que recibe)
- Conclusiones y recomendaciones"""
    },
    'pai': {
        'title': 'Plan de Atencion Individualizado (PAI)',
        'prompt': """Genera un borrador de Plan de Atencion Individualizado (PAI) del residente.
Secciones:
- Datos identificativos (nombre, habitacion, diagnosticos, alergias, medicacion)
- Valoracion integral:
  * Area funcional (Barthel, nivel de dependencia, movilidad)
  * Area clinica (diagnosticos, constantes vitales, riesgo de UPP segun Norton)
  * Area social (grupo asignado, participacion)
- Objetivos de atencion (basados en los datos — funcionales, clinicos, sociales)
- Plan de intervenciones (que atenciones necesita, frecuencia recomendada, profesionales implicados)
- Seguimiento (indicadores a vigilar, frecuencia de revision)
Nota: este es un borrador basado en datos del sistema. Debe ser revisado y completado por el equipo interdisciplinar."""
    },
}

@bp.route('/api/resident/<int:resident_id>/inspection-report', methods=['POST'])
@admin_required
def inspection_report(resident_id: int):
    """Generate formal inspection report for a resident."""
    data = request.get_json() or {}
    report_type = data.get('type', 'monthly')

    if report_type not in REPORT_TYPES:
        return jsonify({'error': 'Tipo de informe no valido'}), 400

    rtype = REPORT_TYPES[report_type]
    ctx = _build_resident_context(resident_id, days=30)
    if not ctx:
        return jsonify({'error': 'Residente no encontrado'}), 404

    resident = db.session.get(Resident, resident_id)
    context_text = _context_to_text(ctx, 30)
    prompt = f"{rtype['prompt']}\n\n{context_text}"

    try:
        html = _call_claude(INSPECTION_SYSTEM, prompt)
        html = html.strip()
        if html.startswith('```'):
            html = html.split('\n', 1)[1] if '\n' in html else html[3:]
        if html.endswith('```'):
            html = html.rsplit('```', 1)[0]
        html = html.strip()
    except Exception as e:
        return jsonify({'error': f'Error: {str(e)}'}), 500

    return jsonify({
        'html': html,
        'title': rtype['title'],
        'resident_name': resident.name if resident else '',
        'resident_room': resident.room_number or '' if resident else '',
        'generated_at': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'generated_by': current_user.name if current_user else '',
    })


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

    if days == 0:
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
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

    # Activities
    activity_data = []
    participations = ActivityParticipation.query.join(Activity).filter(
        ActivityParticipation.resident_id == resident_id,
        Activity.activity_date >= cutoff.date(),
    ).all()
    for p in participations:
        a = p.activity
        if a:
            eng_label = {'participated': 'Ha participado', 'passive': 'Pasivo', 'refused': 'Ha rechazado', 'absent': 'Ausente'}.get(p.engagement, 'Pendiente')
            activity_data.append({
                'date': a.activity_date.strftime('%d/%m/%Y'),
                'title': a.title,
                'category': a.category,
                'engagement': eng_label,
            })

    # Mood records
    from ..models import MoodRecord
    import json as _json
    mood_data = []
    mood_records = MoodRecord.query.filter(
        MoodRecord.resident_id == resident_id,
        MoodRecord.recorded_at >= cutoff,
    ).order_by(MoodRecord.recorded_at.desc()).limit(20).all()
    mood_labels = {1: 'Muy mal', 2: 'Mal', 3: 'Normal', 4: 'Bien', 5: 'Muy bien'}
    for m in mood_records:
        flags = _json.loads(m.behavior_flags) if m.behavior_flags else []
        mood_data.append({
            'date': m.recorded_at.strftime('%d/%m/%Y %H:%M'),
            'score': m.mood_score,
            'label': mood_labels.get(m.mood_score, '?'),
            'flags': ', '.join(flags) if flags else '',
            'notes': m.notes or '',
            'worker': m.worker.name if m.worker else '?',
        })

    return {
        'profile': profile,
        'care_records': care_data,
        'vital_signs': vital_data,
        'assessments': assess_data,
        'incidents': incident_data,
        'cleaning': cleaning_data,
        'activities': activity_data,
        'mood': mood_data,
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

    if ctx.get('activities'):
        lines.append(f"\nACTIVIDADES ({len(ctx['activities'])}):")
        for act in ctx['activities'][:10]:
            lines.append(f"  {act['date']} - {act['title']} ({act['category']}) — {act['engagement']}")

    if ctx.get('mood'):
        lines.append(f"\nESTADO DE ANIMO ({len(ctx['mood'])} registros):")
        for m in ctx['mood'][:10]:
            flags_str = f" [{m['flags']}]" if m['flags'] else ''
            notes_str = f" — {m['notes']}" if m['notes'] else ''
            lines.append(f"  {m['date']} - {m['label']} ({m['score']}/5){flags_str}{notes_str} por {m['worker']}")

    if ctx['worker_notes']:
        lines.append(f"\nNOTAS DEL PERSONAL:")
        for note in ctx['worker_notes'][:15]:
            lines.append(f"  - {note}")

    return '\n'.join(lines)


# ── AI summary endpoint ──────────────────────────────────────────────────────

SUMMARY_SYSTEM = """Eres un asistente de la residencia de ancianos La Vila Gran.
Genera informes estructurados en HTML sobre residentes basandote en los datos proporcionados.
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
- Actividades y participacion (si hay datos de actividades realizadas)
- Aspectos a vigilar / recomendaciones (si detectas algo que requiera atencion)

Usa <strong> para datos importantes, <ul>/<li> para listas.
Si una seccion no tiene datos, omitela. No uses emojis."""

@bp.route('/api/resident/<int:resident_id>/ai-summary', methods=['POST'])
@admin_required
def ai_summary(resident_id: int):
    """Generate AI clinical report for a resident."""
    data = request.get_json() or {}
    period = data.get('period', 'week')
    days_map = {'today': 0, 'week': 7, 'month': 30}
    days = days_map.get(period, 7)

    ctx = _build_resident_context(resident_id, days)
    if not ctx:
        return jsonify({'error': 'Residente no encontrado'}), 404

    resident = db.session.get(Resident, resident_id)
    context_text = _context_to_text(ctx, days)
    period_label = {'today': 'de hoy', 'week': 'de la ultima semana', 'month': 'del ultimo mes'}
    prompt = f"""Genera un informe {period_label.get(period, 'reciente')} de este residente.

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


# ── Family-friendly AI summary ─────────────────────────────────────────────────

FAMILY_SUMMARY_SYSTEM = """Eres un asistente de la residencia de ancianos La Vila Gran.
Genera un resumen semanal PARA LA FAMILIA del residente, en un tono amable, cercano y positivo.

REGLAS IMPORTANTES:
- Escribe siempre en espanol
- Usa un tono calido y tranquilizador, como si hablaras con un familiar en una visita
- NO uses terminologia clinica ni datos en bruto de constantes vitales
- Si hay constantes vitales normales, di "sus constantes se mantienen estables"
- Solo menciona anomalias de forma suave: "hemos prestado especial atencion a..."
- Destaca las actividades en las que ha participado y su nivel de disfrute
- Menciona la interaccion social y el estado de animo
- Si hay mejoras, destácalas; si hay declive, sé delicado y propositivo
- NO incluyas diagnosticos ni medicacion especifica
- NO uses emojis

FORMATO: Devuelve SOLO contenido HTML (sin <html>, <head> ni <body>).
Estructura libre pero cercana: un saludo inicial, 2-3 parrafos sobre como ha ido la semana,
y un cierre positivo. Usa <strong> para lo mas relevante. Maximo 300 palabras."""


@bp.route('/api/resident/<int:resident_id>/family-summary', methods=['POST'])
@admin_required
def family_summary(resident_id: int):
    """Generate a family-friendly weekly summary for a resident."""
    ctx = _build_resident_context(resident_id, 7)
    if not ctx:
        return jsonify({'error': 'Residente no encontrado'}), 404

    resident = db.session.get(Resident, resident_id)
    context_text = _context_to_text(ctx, 7)
    prompt = f"""Genera un resumen semanal para la familia de {resident.name}.
Los datos son internos — tu resumen debe ser PARA LA FAMILIA, sin jerga clinica.

{context_text}"""

    try:
        html = _call_claude(FAMILY_SUMMARY_SYSTEM, prompt)
        html = html.strip()
        if html.startswith('```'):
            html = html.split('\n', 1)[1] if '\n' in html else html[3:]
        if html.endswith('```'):
            html = html.rsplit('```', 1)[0]
        html = html.strip()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'html': html,
        'title': f'Resumen familiar — {resident.name}',
        'resident_name': resident.name if resident else '',
        'generated_at': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'generated_by': current_user.name if current_user else '',
    })


# ── Global AI report ─────────────────────────────────────────────────────────

GLOBAL_REPORT_SYSTEM = """Eres un asistente de direccion de la residencia de ancianos La Vila Gran.
Genera informes ejecutivos estructurados en HTML sobre el estado general de la residencia.
Escribe siempre en espanol. Tono profesional y conciso.
No inventes datos — solo comenta lo que aparece en los datos.

FORMATO: Devuelve SOLO el contenido HTML (sin <html>, <head> ni <body>).
Secciones con h3:
- Resumen ejecutivo (estado general de la residencia en un parrafo)
- Actividad asistencial (atenciones por tipo, frecuencia, trabajadores mas activos)
- Estado de los residentes (residentes que requieren mas atencion, cambios funcionales)
- Constantes vitales destacables (alertas, tendencias)
- Incidencias (resumen de incidencias del periodo)
- Limpieza y mantenimiento (cobertura, habitaciones pendientes)
- Observaciones del personal (notas relevantes agrupadas por tematica)
- Recomendaciones (acciones sugeridas para la direccion)

Usa <strong>, <ul>/<li>, <table> cuando sea apropiado. No uses emojis.
Si una seccion no tiene datos, omitela."""

@bp.route('/api/global-ai-report', methods=['POST'])
@admin_required
def global_ai_report():
    """Generate AI report for the entire residence."""
    data = request.get_json() or {}
    period = data.get('period', 'week')
    days_map = {'today': 0, 'week': 7, 'month': 30}
    days = days_map.get(period, 7)
    if period == 'today':
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        cutoff = datetime.now() - timedelta(days=days)

    # Gather global data
    lines = []

    # Active residents and workers
    residents = Resident.query.filter_by(active=True).all()
    workers = Cleaner.query.filter_by(active=True).all()
    lines.append(f"RESIDENCIA LA VILA GRAN — Fecha: {datetime.now().strftime('%d/%m/%Y')} — {len(residents)} residentes activos, {len(workers)} trabajadores activos")

    # Care records summary
    care_records = CareRecord.query.filter(
        CareRecord.start_time >= cutoff, CareRecord.end_time.isnot(None),
    ).all()
    lines.append(f"\nATENCIONES: {len(care_records)} en los ultimos {days} dias")

    # Group by care type
    type_counts = {}
    worker_counts = {}
    resident_counts = {}
    notes_list = []
    for cr in care_records:
        for ct in cr.care_types:
            type_counts[ct.name] = type_counts.get(ct.name, 0) + 1
        if cr.worker:
            worker_counts[cr.worker.name] = worker_counts.get(cr.worker.name, 0) + 1
        if cr.resident:
            resident_counts[cr.resident.name] = resident_counts.get(cr.resident.name, 0) + 1
        if cr.notes:
            r_name = cr.resident.name if cr.resident else '?'
            lines_note = f"  {cr.start_time.strftime('%d/%m')}: {r_name} — {cr.notes}"
            notes_list.append(lines_note)

    if type_counts:
        lines.append("Por tipo: " + ', '.join(f"{k}: {v}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1])[:10]))
    if worker_counts:
        lines.append("Por trabajador: " + ', '.join(f"{k}: {v}" for k, v in sorted(worker_counts.items(), key=lambda x: -x[1])[:10]))
    if resident_counts:
        top_residents = sorted(resident_counts.items(), key=lambda x: -x[1])[:10]
        lines.append("Residentes con mas atenciones: " + ', '.join(f"{k}: {v}" for k, v in top_residents))

    # Vital signs
    vital_readings = db.session.query(VitalSignReading).join(CareRecord).filter(
        CareRecord.start_time >= cutoff,
    ).all()
    vital_summary = {}
    for r in vital_readings:
        vst = r.vital_sign_type
        if not vst:
            continue
        key = vst.name
        if key not in vital_summary:
            vital_summary[key] = {'values': [], 'unit': vst.unit, 'min': vst.min_value, 'max': vst.max_value}
        vital_summary[key]['values'].append(r.value)

    if vital_summary:
        lines.append(f"\nCONSTANTES VITALES ({len(vital_readings)} lecturas):")
        for name, info in vital_summary.items():
            vals = info['values']
            avg = sum(vals) / len(vals)
            out_of_range = sum(1 for v in vals if (info['min'] is not None and v < info['min']) or (info['max'] is not None and v > info['max']))
            lines.append(f"  {name}: media {avg:.1f} {info['unit']}, {len(vals)} lecturas, {out_of_range} fuera de rango")

    # Assessments
    assessments = AssessmentRecord.query.filter(AssessmentRecord.assessed_at >= cutoff).all()
    if assessments:
        lines.append(f"\nVALORACIONES ({len(assessments)}):")
        for a in assessments:
            r_name = a.resident.name if a.resident else '?'
            lines.append(f"  {a.assessed_at.strftime('%d/%m')} - {r_name}: {a.scale_type.capitalize()} {a.score} ({a.interpretation})")

    # Incidents
    incidents = Incident.query.filter(Incident.created_at >= cutoff).all()
    if incidents:
        lines.append(f"\nINCIDENCIAS ({len(incidents)}):")
        for inc in incidents:
            r_name = inc.resident.name if inc.resident else '—'
            lines.append(f"  {inc.created_at.strftime('%d/%m')} - {inc.title} (sev: {inc.severity}, estado: {inc.status}, residente: {r_name})")

    # Cleaning
    cleanings = CleaningRecord.query.filter(
        CleaningRecord.start_time >= cutoff, CleaningRecord.end_time.isnot(None),
    ).all()
    lines.append(f"\nLIMPIEZAS: {len(cleanings)} en los ultimos {days} dias")
    for cl in cleanings:
        if cl.notes:
            room_num = cl.room.number if cl.room else '?'
            notes_list.append(f"  {cl.start_time.strftime('%d/%m')}: Hab. {room_num} (limpieza) — {cl.notes}")

    # Activities
    period_activities = Activity.query.filter(
        Activity.activity_date >= cutoff.date(),
    ).options(joinedload(Activity.participations)).all()
    if period_activities:
        lines.append(f"\nACTIVIDADES ({len(period_activities)}):")
        for a in period_activities:
            confirmed = sum(1 for p in a.participations if p.engagement == 'participated')
            total = len(a.participations)
            names = [p.resident.name for p in a.participations if p.engagement == 'participated' and p.resident]
            absent = [p.resident.name for p in a.participations if p.engagement == 'absent' and p.resident]
            lines.append(f"  {a.activity_date.strftime('%d/%m')} - {a.title} ({a.category}) — {confirmed}/{total} participants")
            if names:
                lines.append(f"    Participants: {', '.join(names)}")
            if absent:
                lines.append(f"    Absents: {', '.join(absent)}")

    # Worker notes
    if notes_list:
        lines.append(f"\nNOTAS DEL PERSONAL ({len(notes_list)}):")
        for n in notes_list[:30]:
            lines.append(n)

    context_text = '\n'.join(lines)
    period_label = {'today': 'de hoy', 'week': 'de la ultima semana', 'month': 'del ultimo mes'}
    prompt = f"Genera un informe ejecutivo {period_label.get(period, 'reciente')} de la residencia.\n\n{context_text}"

    try:
        html = _call_claude(GLOBAL_REPORT_SYSTEM, prompt)
        html = html.strip()
        if html.startswith('```'):
            html = html.split('\n', 1)[1] if '\n' in html else html[3:]
        if html.endswith('```'):
            html = html.rsplit('```', 1)[0]
        html = html.strip()
    except Exception as e:
        return jsonify({'error': f'Error al generar informe: {str(e)}'}), 500

    period_titles = {'today': 'Informe del dia', 'week': 'Informe semanal', 'month': 'Informe mensual'}
    return jsonify({
        'html': html,
        'period': period,
        'title': period_titles.get(period, 'Informe'),
        'generated_at': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'generated_by': current_user.name if current_user else '',
    })


# ── Shift Handover Report ─────────────────────────────────────────────────────

HANDOVER_SYSTEM = """Eres un asistente de la residencia de ancianos La Vila Gran.
Genera un informe de traspaso de turno en HTML estructurado para el equipo entrante.
El informe debe ser claro, conciso y orientado a la accion.
Escribe en espanol. No inventes datos.

REGLA CRITICA: Si una atencion incluye constantes vitales (SpO2, temperatura, TA, glucemia, etc.)
DEBES mostrar los valores numericos exactos en el informe. Ejemplo:
"Maria Garcia — Constantes vitales: SpO2 96%, TA 130/80mmHg, Temp 36.5°C"
NUNCA omitas los valores de constantes vitales, son esenciales para el equipo entrante.

FORMATO: Solo contenido HTML (sin <html>/<body>).
Secciones con h3:
- Resumen del turno (que ha pasado, cuantos trabajadores, atenciones y limpiezas)
- Constantes vitales registradas (TODAS las constantes del turno con valores numericos por residente. Esta seccion es OBLIGATORIA si hay constantes)
- Atenciones destacadas (las que tienen notas, duracion inusual o constantes fuera de rango)
- Incidencias (nuevas o abiertas)
- Alertas y avisos (constantes fuera de rango con valores, sesiones sin cerrar)
- Actividades realizadas (si hay actividades programadas, participacion)
- Pendiente para el siguiente turno (que queda por hacer)
- Observaciones del personal (notas agrupadas por tematica)

Usa <strong>, <ul>/<li>. No uses emojis. Se breve y directo."""

@bp.route('/api/shift-handover-report', methods=['POST'])
@admin_required
def shift_handover_report():
    """Generate AI shift handover summary."""
    from ..models import ShiftType, ShiftAssignment, Absence

    data = request.get_json() or {}
    hours = data.get('hours', 8)
    now = datetime.now()
    cutoff = now - timedelta(hours=hours)
    today = now.date()

    lines = []

    # Workers on shift today
    shifts = ShiftAssignment.query.options(
        joinedload(ShiftAssignment.cleaner), joinedload(ShiftAssignment.shift_type),
    ).filter(ShiftAssignment.date == today, ShiftAssignment.shift_type_id.isnot(None)).all()
    absent_ids = {a.cleaner_id for a in Absence.query.filter(
        Absence.start_date <= today, Absence.end_date >= today).all()}
    on_shift = [s for s in shifts if s.cleaner_id not in absent_ids]
    shift_names = set(s.shift_type.short_name for s in on_shift if s.shift_type)
    lines.append(f"TRASPASO DE TURNO — {now.strftime('%d/%m/%Y %H:%M')}")
    lines.append(f"Trabajadores en turno hoy: {len(on_shift)} ({', '.join(shift_names)})")

    # Care records in last N hours
    care_records = CareRecord.query.options(
        joinedload(CareRecord.worker), joinedload(CareRecord.resident),
        joinedload(CareRecord.care_types),
        joinedload(CareRecord.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
    ).filter(CareRecord.start_time >= cutoff).order_by(CareRecord.start_time.desc()).all()

    lines.append(f"\nATENCIONES ULTIMAS {hours}H: {len(care_records)}")
    worker_care = {}
    notes_list = []
    vital_alerts = []
    for cr in care_records:
        w_name = cr.worker.name if cr.worker else '?'
        worker_care[w_name] = worker_care.get(w_name, 0) + 1
        types = ', '.join(ct.name for ct in cr.care_types) if cr.care_types else '?'
        r_name = cr.resident.name if cr.resident else '?'
        dur = cr.calculate_duration()
        dur_str = f" ({round(dur/60)}min)" if dur else ''
        # Build vital signs inline string for this care record
        vitals_parts = []
        for reading in cr.vital_sign_readings:
            vst = reading.vital_sign_type
            if vst:
                flag = ''
                if ((vst.min_value is not None and reading.value < vst.min_value) or
                        (vst.max_value is not None and reading.value > vst.max_value)):
                    flag = ' ⚠️'
                    vital_alerts.append(
                        f"  {r_name}: {vst.name} = {reading.value} {vst.unit}"
                        f" (rango: {vst.min_value}-{vst.max_value})")
                vitals_parts.append(f"{vst.name}: {reading.value}{vst.unit}{flag}")

        vitals_str = f" | {', '.join(vitals_parts)}" if vitals_parts else ''
        lines.append(
            f"  {cr.start_time.strftime('%H:%M')} - {r_name}: {types}"
            f" por {w_name}{dur_str}{vitals_str}")
        if cr.notes:
            notes_list.append(f"  {cr.start_time.strftime('%H:%M')} {r_name} ({w_name}): {cr.notes}")

    if worker_care:
        lines.append("Por trabajador: " + ', '.join(f"{k}: {v}" for k, v in sorted(worker_care.items(), key=lambda x: -x[1])))

    # Cleaning in last N hours
    cleanings = CleaningRecord.query.options(
        joinedload(CleaningRecord.room), joinedload(CleaningRecord.cleaner),
    ).filter(
        CleaningRecord.start_time >= cutoff, CleaningRecord.end_time.isnot(None),
    ).order_by(CleaningRecord.start_time.desc()).all()
    lines.append(f"\nLIMPIEZAS ULTIMAS {hours}H: {len(cleanings)}")
    for cl in cleanings:
        room_num = cl.room.number if cl.room else '?'
        floor_name = cl.room.floor.name if cl.room and cl.room.floor else ''
        w_name = cl.cleaner.name if cl.cleaner else '?'
        dur = cl.calculate_duration()
        dur_str = f" ({round(dur/60)}min)" if dur else ''
        lines.append(f"  {cl.start_time.strftime('%H:%M')} - Hab.{room_num} {floor_name} por {w_name}{dur_str}")
        if cl.notes:
            notes_list.append(f"  {cl.start_time.strftime('%H:%M')} Hab.{room_num} (limpieza): {cl.notes}")

    # Open sessions (not closed)
    open_care = CareRecord.query.filter(CareRecord.end_time.is_(None)).count()
    open_clean = CleaningRecord.query.filter(CleaningRecord.end_time.is_(None)).count()
    if open_care or open_clean:
        lines.append(f"\nSESIONES ABIERTAS: {open_care} atenciones, {open_clean} limpiezas")

    # Recent incidents
    incidents = Incident.query.filter(Incident.created_at >= cutoff).order_by(Incident.created_at.desc()).all()
    open_incidents = Incident.query.filter(Incident.status.in_(['open', 'in_progress'])).all()
    if incidents or open_incidents:
        lines.append(f"\nINCIDENCIAS: {len(incidents)} nuevas, {len(open_incidents)} abiertas")
        for inc in incidents:
            r_name = inc.resident.name if inc.resident else '—'
            lines.append(f"  {inc.created_at.strftime('%H:%M')} - {inc.title} (sev: {inc.severity}, residente: {r_name})")
        for inc in open_incidents:
            if inc not in incidents:
                lines.append(f"  ABIERTA: {inc.title} (desde {inc.created_at.strftime('%d/%m')})")

    if vital_alerts:
        lines.append(f"\nALERTAS CONSTANTES VITALES:")
        for va in vital_alerts:
            lines.append(va)

    # Activities today
    today_activities = Activity.query.filter_by(activity_date=today).options(
        joinedload(Activity.participations),
    ).all()
    if today_activities:
        lines.append(f"\nACTIVIDADES DE HOY ({len(today_activities)}):")
        for a in today_activities:
            confirmed = sum(1 for p in a.participations if p.engagement == 'participated')
            total = len(a.participations)
            time_str = a.start_time.strftime('%H:%M') if a.start_time else ''
            names = [p.resident.name for p in a.participations if p.engagement == 'participated' and p.resident]
            absent = [p.resident.name for p in a.participations if p.engagement == 'absent' and p.resident]
            lines.append(f"  {time_str} {a.title} ({a.category}) — {confirmed}/{total} confirmados")
            if names:
                lines.append(f"    Participants: {', '.join(names)}")
            if absent:
                lines.append(f"    Absents: {', '.join(absent)}")

    if notes_list:
        lines.append(f"\nNOTAS DEL PERSONAL:")
        for n in notes_list[:20]:
            lines.append(n)

    context = '\n'.join(lines)
    prompt = f"Genera un informe de traspaso de turno basado en estos datos:\n\n{context}"

    try:
        html = _call_claude(HANDOVER_SYSTEM, prompt)
        html = html.strip()
        if html.startswith('```'):
            html = html.split('\n', 1)[1] if '\n' in html else html[3:]
        if html.endswith('```'):
            html = html.rsplit('```', 1)[0]
        html = html.strip()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'html': html,

        'title': f'Traspaso de turno — {now.strftime("%d/%m/%Y %H:%M")}',
        'generated_at': now.strftime('%d/%m/%Y %H:%M'),
        'generated_by': current_user.name if current_user else '',
    })


# ── Compliance Audit ─────────────────────────────────────────────────────────

@bp.route('/admin/compliance-audit')
@admin_required
def compliance_audit():
    """Show compliance audit dashboard with rule-based checks."""
    from ..models import (ShiftAssignment, ShiftCoverageRequirement,
                          LegalDocument, DocumentSignature)
    from .assessments import _call_claude
    import json as _json2

    now = datetime.now()
    findings = []

    # 1. Residents without recent Barthel/Norton (>6 months)
    cutoff_6m = now - timedelta(days=180)
    residents = Resident.query.filter_by(active=True).all()
    for r in residents:
        latest_b = AssessmentRecord.query.filter_by(resident_id=r.id, scale_type='barthel').order_by(AssessmentRecord.assessed_at.desc()).first()
        latest_n = AssessmentRecord.query.filter_by(resident_id=r.id, scale_type='norton').order_by(AssessmentRecord.assessed_at.desc()).first()
        if not latest_b or latest_b.assessed_at < cutoff_6m:
            days = (now - latest_b.assessed_at).days if latest_b else None
            findings.append({'category': 'valoraciones', 'severity': 'warning',
                'text': f'{r.name}: sin Barthel en {"mas de " + str(days) + " dias" if days else "nunca valorado"}',
                'link': f'/admin/resident/{r.id}'})
        if not latest_n or latest_n.assessed_at < cutoff_6m:
            days = (now - latest_n.assessed_at).days if latest_n else None
            findings.append({'category': 'valoraciones', 'severity': 'warning',
                'text': f'{r.name}: sin Norton en {"mas de " + str(days) + " dias" if days else "nunca valorado"}',
                'link': f'/admin/resident/{r.id}'})

    # 2. Norton ≤12 (high risk UPP)
    high_risk_norton = AssessmentRecord.query.filter(
        AssessmentRecord.scale_type == 'norton', AssessmentRecord.score <= 12,
    ).order_by(AssessmentRecord.assessed_at.desc()).all()
    seen_residents = set()
    for a in high_risk_norton:
        if a.resident_id in seen_residents:
            continue
        seen_residents.add(a.resident_id)
        r = a.resident
        if r and r.active:
            findings.append({'category': 'clinico', 'severity': 'critical',
                'text': f'{r.name}: Norton {a.score}/20 — riesgo {"muy alto" if a.score <= 9 else "alto"} de UPP',
                'link': f'/admin/resident/{a.resident_id}'})

    # 3. Residents without care in >3 days
    cutoff_3d = now - timedelta(days=3)
    for r in residents:
        last_care = CareRecord.query.filter(
            CareRecord.resident_id == r.id, CareRecord.end_time.isnot(None),
        ).order_by(CareRecord.start_time.desc()).first()
        if not last_care or last_care.start_time < cutoff_3d:
            days = (now - last_care.start_time).days if last_care else None
            findings.append({'category': 'atenciones', 'severity': 'warning',
                'text': f'{r.name}: sin atencion registrada en {str(days) + " dias" if days else "nunca"}',
                'link': f'/admin/resident/{r.id}'})

    # 4. Unsigned legal documents
    total_workers = Cleaner.query.filter_by(active=True).count()
    active_docs = LegalDocument.query.filter_by(active=True).all()
    for doc in active_docs:
        signed = DocumentSignature.query.filter_by(document_id=doc.id).count()
        pending = total_workers - signed
        if pending > 0:
            findings.append({'category': 'documentos', 'severity': 'info',
                'text': f'Documento "{doc.title}": {pending} trabajadores sin firmar',
                'link': '/admin/documents'})

    # 5. Open incidents >7 days
    cutoff_7d = now - timedelta(days=7)
    old_incidents = Incident.query.filter(
        Incident.status.in_(['open', 'in_progress']), Incident.created_at < cutoff_7d,
    ).all()
    for inc in old_incidents:
        days = (now - inc.created_at).days
        findings.append({'category': 'incidencias', 'severity': 'warning',
            'text': f'Incidencia "{inc.title}" abierta hace {days} dias (sev: {inc.severity})',
            'link': '/admin/incidents'})

    # 6. Incomplete training
    from ..models import TrainingPill, TrainingCompletion
    pills = TrainingPill.query.filter_by(active=True).all()
    workers = Cleaner.query.filter_by(active=True).all()
    for pill in pills:
        completed_ids = {tc.cleaner_id for tc in TrainingCompletion.query.filter_by(
            training_pill_id=pill.id, passed=True).all()}
        pending = [w for w in workers if w.id not in completed_ids]
        if len(pending) > 0:
            findings.append({'category': 'formacion', 'severity': 'info',
                'text': f'Formacion "{pill.title}": {len(pending)} trabajadores pendientes',
                'link': '/admin/training'})

    # 7. PAI coverage — residents with active care plan
    for r in residents:
        active_pai = CarePlan.query.filter_by(resident_id=r.id, status='active').first()
        if not active_pai:
            findings.append({'category': 'pai', 'severity': 'info',
                'text': f'{r.name}: sin PAI activo',
                'link': f'/admin/resident/{r.id}'})
        elif active_pai.review_date and active_pai.review_date < now.date():
            days_overdue = (now.date() - active_pai.review_date).days
            findings.append({'category': 'pai', 'severity': 'warning',
                'text': f'{r.name}: PAI pendiente de revisión (vencido hace {days_overdue} días)',
                'link': f'/admin/resident/{r.id}'})

    # 8. Incident response time — open incidents severity high/critical
    urgent_open = Incident.query.filter(
        Incident.status.in_(['open', 'in_progress']),
        Incident.severity.in_(['high', 'critical']),
    ).all()
    for inc in urgent_open:
        days = (now - inc.created_at).days
        if days >= 3:
            findings.append({'category': 'incidencias', 'severity': 'critical',
                'text': f'Incidencia URGENTE "{inc.title}" abierta hace {days} días sin resolver',
                'link': '/admin/incidents'})

    # ── Compliance scoring ──
    total_residents = len(residents)
    scores = {}

    # Valoraciones score
    assessed_b = sum(1 for r in residents if AssessmentRecord.query.filter(
        AssessmentRecord.resident_id == r.id, AssessmentRecord.scale_type == 'barthel',
        AssessmentRecord.assessed_at >= cutoff_6m).first())
    assessed_n = sum(1 for r in residents if AssessmentRecord.query.filter(
        AssessmentRecord.resident_id == r.id, AssessmentRecord.scale_type == 'norton',
        AssessmentRecord.assessed_at >= cutoff_6m).first())
    scores['valoraciones'] = round(((assessed_b + assessed_n) / (total_residents * 2)) * 100) if total_residents else 100

    # PAI coverage score
    pai_count = sum(1 for r in residents if CarePlan.query.filter_by(
        resident_id=r.id, status='active').first())
    scores['pai'] = round((pai_count / total_residents) * 100) if total_residents else 100

    # Atenciones score (residents with care in last 3 days)
    cared = sum(1 for r in residents if CareRecord.query.filter(
        CareRecord.resident_id == r.id, CareRecord.start_time >= cutoff_3d).first())
    scores['atenciones'] = round((cared / total_residents) * 100) if total_residents else 100

    # Documentos score
    if active_docs and total_workers:
        total_sigs = sum(DocumentSignature.query.filter_by(document_id=d.id).count() for d in active_docs)
        max_sigs = len(active_docs) * total_workers
        scores['documentos'] = round((total_sigs / max_sigs) * 100) if max_sigs else 100
    else:
        scores['documentos'] = 100

    # Incidencias score (no urgent open incidents)
    open_count = Incident.query.filter(Incident.status.in_(['open', 'in_progress'])).count()
    scores['incidencias'] = 100 if open_count == 0 else max(0, 100 - open_count * 15)

    # Formacion score
    if pills and workers:
        total_completions = sum(TrainingCompletion.query.filter_by(
            training_pill_id=p.id, passed=True).count() for p in pills)
        max_completions = len(pills) * len(workers)
        scores['formacion'] = round((total_completions / max_completions) * 100) if max_completions else 100
    else:
        scores['formacion'] = 100

    # Global score (weighted average)
    weights = {'valoraciones': 25, 'pai': 20, 'atenciones': 20, 'documentos': 10, 'incidencias': 15, 'formacion': 10}
    global_score = sum(scores.get(k, 100) * w for k, w in weights.items()) / sum(weights.values())
    global_score = round(global_score)

    # Group by category
    categories = {}
    for f in findings:
        cat = f['category']
        categories.setdefault(cat, []).append(f)

    cat_labels = {'valoraciones': 'Valoraciones clínicas', 'clinico': 'Riesgos clínicos',
                  'atenciones': 'Atenciones', 'documentos': 'Documentos legales',
                  'incidencias': 'Incidencias', 'formacion': 'Formación', 'pai': 'Planes de cuidado (PAI)'}
    cat_icons = {'valoraciones': 'bi-clipboard2-pulse', 'clinico': 'bi-heart-pulse',
                 'atenciones': 'bi-person-heart', 'documentos': 'bi-file-earmark-text',
                 'incidencias': 'bi-exclamation-triangle', 'formacion': 'bi-mortarboard',
                 'pai': 'bi-clipboard-check'}

    return render_template('admin_compliance_audit.html',
        findings=findings, categories=categories,
        cat_labels=cat_labels, cat_icons=cat_icons,
        scores=scores, global_score=global_score,
        total_findings=len(findings),
        critical_count=sum(1 for f in findings if f['severity'] == 'critical'),
        warning_count=sum(1 for f in findings if f['severity'] == 'warning'),
    )


@bp.route('/api/compliance/ai-report', methods=['POST'])
@admin_required
def compliance_ai_report():
    """Generate AI narrative report of compliance status."""
    from ..models import (ShiftAssignment, ShiftCoverageRequirement,
                          LegalDocument, DocumentSignature, TrainingPill, TrainingCompletion)

    now = datetime.now()
    residents = Resident.query.filter_by(active=True).all()
    total = len(residents)

    # Gather key metrics
    cutoff_6m = now - timedelta(days=180)
    barthel_ok = sum(1 for r in residents if AssessmentRecord.query.filter(
        AssessmentRecord.resident_id == r.id, AssessmentRecord.scale_type == 'barthel',
        AssessmentRecord.assessed_at >= cutoff_6m).first())
    norton_ok = sum(1 for r in residents if AssessmentRecord.query.filter(
        AssessmentRecord.resident_id == r.id, AssessmentRecord.scale_type == 'norton',
        AssessmentRecord.assessed_at >= cutoff_6m).first())
    pai_ok = sum(1 for r in residents if CarePlan.query.filter_by(
        resident_id=r.id, status='active').first())
    open_incidents = Incident.query.filter(Incident.status.in_(['open', 'in_progress'])).count()
    falls_30d = Incident.query.filter(Incident.is_fall == True, Incident.created_at >= now - timedelta(days=30)).count()

    lines = [
        f"INFORME DE CUMPLIMIENTO — {now.strftime('%d/%m/%Y')}",
        f"Residentes activos: {total}",
        f"Barthel al día (<6 meses): {barthel_ok}/{total}",
        f"Norton al día (<6 meses): {norton_ok}/{total}",
        f"PAIs activos: {pai_ok}/{total}",
        f"Incidencias abiertas: {open_incidents}",
        f"Caídas últimos 30 días: {falls_30d}",
    ]

    context = '\n'.join(lines)
    system = """Eres un consultor de calidad asistencial para la residencia de ancianos La Vila Gran.
Genera un informe narrativo breve de cumplimiento normativo en HTML (sin <html>/<body>).
Secciones con h4:
- Resumen ejecutivo (puntuación general, estado)
- Áreas de cumplimiento (qué va bien)
- Áreas de mejora (qué necesita atención urgente)
- Recomendaciones concretas (acciones a tomar)
Usa colores: verde para bien, naranja para mejorable, rojo para urgente.
Escribe en español. Sé breve y directo."""

    try:
        html = _call_claude(system, f"Datos de cumplimiento:\n\n{context}")
        html = html.strip()
        if html.startswith('```'):
            html = html.split('\n', 1)[1] if '\n' in html else html[3:]
        if html.endswith('```'):
            html = html.rsplit('```', 1)[0]
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'html': html.strip()})


# ── Risk Profile per Resident ────────────────────────────────────────────────

@bp.route('/api/resident/<int:resident_id>/risk-profile')
@admin_required
def risk_profile(resident_id: int):
    """Calculate risk badges using ML + rule-based hybrid scoring."""
    resident = db.session.get(Resident, resident_id)
    if not resident:
        return jsonify({'error': 'No encontrado'}), 404

    from ..ml_models import predict_risks
    predictions = predict_risks(resident_id)

    if not predictions:
        return jsonify({'risks': {}, 'labels': {}})

    # Backward-compatible format
    risks = {k: v['level'] for k, v in predictions.items() if k != 'features'}
    risk_labels = {
        'caidas': 'Caídas', 'upp': 'UPP',
        'cognitivo': 'Deterioro cognitivo', 'nutricional': 'Nutricional',
    }

    return jsonify({
        'risks': risks,
        'labels': risk_labels,
        'details': {k: v for k, v in predictions.items() if k != 'features'},
        'features': predictions.get('features', {}),
    })


@bp.route('/api/ml/train', methods=['POST'])
@admin_required
def train_ml_models():
    """Train ML models on current historical data."""
    from ..ml_models import train_models
    report = train_models()
    return jsonify(report)


# ── Quality KPIs Dashboard ───────────────────────────────────────────────────

@bp.route('/admin/quality-kpis')
@admin_required
def quality_kpis():
    """Quality indicators dashboard for inspection reporting."""
    from ..models import MedicationAdministration, MedicationPrescription

    days = request.args.get('days', 30, type=int)
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    residents = Resident.query.filter_by(active=True).all()
    total_residents = len(residents)
    resident_days = total_residents * days if total_residents else 1

    # 1. Falls rate (per 1000 resident-days)
    fall_incidents = Incident.query.filter(
        Incident.created_at >= cutoff,
    ).all()
    fall_count = sum(1 for i in fall_incidents if 'ca' in (i.title or '').lower() or 'caida' in (i.title or '').lower() or 'caiguda' in (i.title or '').lower())
    falls_rate = round((fall_count / resident_days) * 1000, 1) if resident_days else 0

    # 2. UPP prevalence (residents with Norton ≤12)
    upp_count = 0
    for r in residents:
        latest_n = AssessmentRecord.query.filter_by(resident_id=r.id, scale_type='norton').order_by(AssessmentRecord.assessed_at.desc()).first()
        if latest_n and latest_n.score <= 12:
            upp_count += 1
    upp_pct = round((upp_count / total_residents) * 100, 1) if total_residents else 0

    # 3. Unplanned weight loss (residents with >5% loss in period)
    weight_loss_count = 0
    for r in residents:
        readings = db.session.query(VitalSignReading.value, CareRecord.start_time).join(
            CareRecord, VitalSignReading.care_record_id == CareRecord.id
        ).join(VitalSignType, VitalSignReading.vital_sign_type_id == VitalSignType.id).filter(
            CareRecord.resident_id == r.id, VitalSignType.name.ilike('%peso%'),
            CareRecord.start_time >= cutoff,
        ).order_by(CareRecord.start_time.asc()).all()
        if len(readings) >= 2 and readings[0].value > 0:
            loss = ((readings[0].value - readings[-1].value) / readings[0].value) * 100
            if loss >= 5:
                weight_loss_count += 1
    weight_loss_pct = round((weight_loss_count / total_residents) * 100, 1) if total_residents else 0

    # 4. Medication refusal rate
    total_admins = MedicationAdministration.query.filter(MedicationAdministration.administered_at >= cutoff).count()
    refused_admins = MedicationAdministration.query.filter(
        MedicationAdministration.administered_at >= cutoff,
        MedicationAdministration.status.in_(['refused', 'omitted']),
    ).count()
    med_refusal_pct = round((refused_admins / total_admins) * 100, 1) if total_admins else 0

    # 5. Assessment compliance (% residents with Barthel+Norton in last 6 months)
    cutoff_6m = now - timedelta(days=180)
    assessed_b = {a.resident_id for a in AssessmentRecord.query.filter(
        AssessmentRecord.scale_type == 'barthel', AssessmentRecord.assessed_at >= cutoff_6m).all()}
    assessed_n = {a.resident_id for a in AssessmentRecord.query.filter(
        AssessmentRecord.scale_type == 'norton', AssessmentRecord.assessed_at >= cutoff_6m).all()}
    both_assessed = assessed_b & assessed_n
    assess_pct = round((len(both_assessed) / total_residents) * 100, 1) if total_residents else 0

    # 6. Training compliance
    from ..models import TrainingPill, TrainingCompletion
    active_pills = TrainingPill.query.filter_by(active=True).count()
    active_workers = db.session.query(Cleaner).filter_by(active=True).count()
    total_needed = active_pills * active_workers
    total_completed = TrainingCompletion.query.filter_by(passed=True).count()
    training_pct = round((total_completed / total_needed) * 100, 1) if total_needed else 100

    # 7. Incident resolution time (avg days)
    resolved = Incident.query.filter(
        Incident.resolved_at.isnot(None), Incident.created_at >= cutoff,
    ).all()
    if resolved:
        avg_resolution = sum((i.resolved_at - i.created_at).days for i in resolved) / len(resolved)
    else:
        avg_resolution = 0

    # 8. Occupancy rate
    from ..models import Room, RoomType
    total_rooms_resident = 0
    try:
        total_rooms_resident = Room.query.join(Room.room_type).filter(
            RoomType.name.ilike('%residen%')
        ).count()
    except Exception:
        pass
    if not total_rooms_resident:
        total_rooms_resident = Room.query.count() or 1
    occupied = len({r.room_number for r in residents if r.room_number})
    occupancy_pct = round((occupied / total_rooms_resident) * 100, 1) if total_rooms_resident else 0

    kpis = [
        {'name': 'Tasa de caídas', 'value': falls_rate, 'unit': 'por 1000 días-residente',
         'target': 5.0, 'icon': 'bi-exclamation-triangle', 'good': 'low'},
        {'name': 'Residentes con riesgo UPP', 'value': upp_pct, 'unit': '%',
         'target': 15, 'icon': 'bi-bandaid', 'good': 'low'},
        {'name': 'Pérdida de peso no planificada', 'value': weight_loss_pct, 'unit': '%',
         'target': 5, 'icon': 'bi-arrow-down-circle', 'good': 'low'},
        {'name': 'Rechazo/omisión medicación', 'value': med_refusal_pct, 'unit': '%',
         'target': 5, 'icon': 'bi-capsule', 'good': 'low'},
        {'name': 'Cumplimiento valoraciones (B+N)', 'value': assess_pct, 'unit': '%',
         'target': 90, 'icon': 'bi-clipboard2-pulse', 'good': 'high'},
        {'name': 'Cumplimiento formativo', 'value': training_pct, 'unit': '%',
         'target': 80, 'icon': 'bi-mortarboard', 'good': 'high'},
        {'name': 'Tiempo resolución incidencias', 'value': round(avg_resolution, 1), 'unit': 'días',
         'target': 7, 'icon': 'bi-clock-history', 'good': 'low'},
        {'name': 'Ocupación', 'value': occupancy_pct, 'unit': '%',
         'target': 85, 'icon': 'bi-house', 'good': 'high'},
    ]

    # Color logic
    for k in kpis:
        if k['good'] == 'low':
            k['color'] = '#198754' if k['value'] <= k['target'] else ('#fd7e14' if k['value'] <= k['target'] * 1.5 else '#dc3545')
        else:
            k['color'] = '#198754' if k['value'] >= k['target'] else ('#fd7e14' if k['value'] >= k['target'] * 0.7 else '#dc3545')

    return render_template('admin_quality_kpis.html',
        kpis=kpis, days=days,
        total_residents=total_residents, fall_count=fall_count,
        upp_count=upp_count, weight_loss_count=weight_loss_count,
    )


# ── Assessment history data for resident detail ──────────────────────────────

def get_resident_assessment_data(resident_id: int) -> dict:
    """Fetch assessment data for the resident detail page."""
    assessments = AssessmentRecord.query.filter_by(
        resident_id=resident_id,
    ).order_by(AssessmentRecord.assessed_at.desc()).all()

    barthel_records = [a for a in assessments if a.scale_type == 'barthel']
    norton_records = [a for a in assessments if a.scale_type == 'norton']

    pfeiffer_records = [a for a in assessments if a.scale_type == 'pfeiffer']

    return {
        'barthel_records': barthel_records,
        'norton_records': norton_records,
        'pfeiffer_records': pfeiffer_records,
    }


# ── Daily Digest ─────────────────────────────────────────────────────────────

DIGEST_SYSTEM = """Eres un asistente de la residencia de ancianos La Vila Gran.
Genera un BREVE resumen diario del residente en HTML (maximo 150 palabras).
Solo incluye lo RELEVANTE del dia: no repitas rutina normal.
Si no ha pasado nada destacable, dilo en una frase.
Marca con <strong> lo importante. No uses emojis.
Si hay alertas (constantes fuera de rango, caidas, rechazo medicacion, cambios de animo), destácalas al inicio.
FORMATO: Solo contenido HTML (sin <html>/<body>). Muy breve."""


@bp.route('/api/daily-digest/generate', methods=['POST'])
@admin_required
def generate_daily_digest():
    """Generate daily AI digest for all residents with activity today."""
    from datetime import date as _date
    today = _date.today()

    # Check if already generated today
    existing = DailyDigest.query.filter_by(date=today).count()
    if existing > 0:
        return jsonify({'message': f'Ya generado hoy ({existing} resúmenes)', 'count': existing})

    residents = Resident.query.filter_by(active=True).all()
    generated = 0

    for resident in residents:
        # Check if resident had any activity today
        ctx = _build_resident_context(resident.id, days=0)  # today only
        has_data = (ctx.get('care_records') or ctx.get('vital_signs') or
                    ctx.get('incidents') or ctx.get('activities') or ctx.get('mood'))
        if not has_data:
            continue

        context_text = _context_to_text(ctx, 0)
        prompt = f"Resumen del dia de hoy para {resident.name}:\n\n{context_text}"

        try:
            html = _call_claude(DIGEST_SYSTEM, prompt)
            html = html.strip()
            if html.startswith('```'):
                html = html.split('\n', 1)[1] if '\n' in html else html[3:]
            if html.endswith('```'):
                html = html.rsplit('```', 1)[0]
            html = html.strip()

            # Detect alerts
            has_alerts = any(kw in html.lower() for kw in ['alerta', 'fuera de rango', 'caida', 'rechazo', 'urgente'])

            digest = DailyDigest(
                resident_id=resident.id, date=today,
                html_content=html, has_alerts=has_alerts,
            )
            db.session.add(digest)
            generated += 1
        except Exception:
            continue

    log_audit('create', 'daily_digest', None,
              {'fecha': today.isoformat(), 'generados': generated})
    ok, error = _safe_commit('Error al guardar los resumenes diarios')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'message': f'{generated} resúmenes generados', 'count': generated})


@bp.route('/admin/daily-digest')
@admin_required
def admin_daily_digest():
    """View daily digests."""
    from datetime import date as _date
    target = request.args.get('date', '')
    view_date = _date.fromisoformat(target) if target else _date.today()

    digests = DailyDigest.query.filter_by(date=view_date).options(
        joinedload(DailyDigest.resident),
    ).order_by(DailyDigest.has_alerts.desc()).all()

    return render_template('admin_daily_digest.html',
        digests=digests, view_date=view_date,
        prev_date=(view_date - timedelta(days=1)).isoformat(),
        next_date=(view_date + timedelta(days=1)).isoformat(),
        is_today=view_date == _date.today(),
    )


# ── Care Plan (PAI) ─────────────────────────────────────────────────────────

PAI_GENERATE_SYSTEM = """Eres un profesional sociosanitario de la residencia La Vila Gran.
Genera un Plan de Atencion Individualizado (PAI) estructurado en HTML.
Basate SOLO en los datos proporcionados. No inventes.

FORMATO (solo HTML, sin <html>/<body>):
- <h3>Datos del residente</h3> (nombre, diagnosticos, dependencia)
- <h3>Objetivos</h3> (lista de objetivos SMART medibles)
- <h3>Intervenciones</h3> (acciones concretas por area: asistencial, social, cognitiva, nutricional, movilidad)
- <h3>Indicadores de seguimiento</h3> (que medir y cuando)
- <h3>Proxima revision</h3> (fecha sugerida, normalmente 3 meses)

Usa <strong>, <ul>/<li>, <table> si es necesario. Escribe en espanol. No uses emojis."""

PAI_REVIEW_SYSTEM = """Eres un profesional sociosanitario de la residencia La Vila Gran.
Compara los objetivos del PAI con los datos reales del periodo.
Indica para cada objetivo: CONSEGUIDO, EN PROGRESO, NO CONSEGUIDO o SIN DATOS.
Sugiere ajustes al plan si es necesario.
FORMATO: HTML breve (sin <html>/<body>). Usa <strong> y colores: verde=conseguido, naranja=en progreso, rojo=no conseguido."""


@bp.route('/api/resident/<int:resident_id>/care-plan/generate', methods=['POST'])
@admin_required
def generate_care_plan(resident_id: int):
    """Generate a new PAI using AI."""
    ctx = _build_resident_context(resident_id, days=30)
    if not ctx:
        return jsonify({'error': 'Residente no encontrado'}), 404

    resident = db.session.get(Resident, resident_id)
    context_text = _context_to_text(ctx, 30)
    prompt = f"Genera un PAI para {resident.name}:\n\n{context_text}"

    try:
        html = _call_claude(PAI_GENERATE_SYSTEM, prompt)
        html = html.strip()
        if html.startswith('```'):
            html = html.split('\n', 1)[1] if '\n' in html else html[3:]
        if html.endswith('```'):
            html = html.rsplit('```', 1)[0]
        html = html.strip()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Save as new care plan
    from datetime import date as _date
    plan = CarePlan(
        resident_id=resident_id,
        html_content=html,
        review_date=(_date.today() + timedelta(days=90)),
        status='active',
        created_by=current_user.id,
    )
    db.session.add(plan)
    ok, error = _safe_flush('Error al guardar el PAI')
    if not ok:
        return jsonify({'error': error}), 500
    log_audit('create', 'care_plan', plan.id, {'resident_id': resident_id})
    ok, error = _safe_commit('Error al guardar el PAI')
    if not ok:
        return jsonify({'error': error}), 500

    return jsonify({
        'html': html,
        'plan_id': plan.id,
        'review_date': plan.review_date.isoformat(),
        'message': 'PAI generado y guardado',
    })


@bp.route('/api/resident/<int:resident_id>/care-plan/review', methods=['POST'])
@admin_required
def review_care_plan(resident_id: int):
    """AI reviews the active care plan against recent data."""
    plan = CarePlan.query.filter_by(
        resident_id=resident_id, status='active',
    ).order_by(CarePlan.created_at.desc()).first()

    if not plan:
        return jsonify({'error': 'No hay PAI activo para este residente'}), 404

    ctx = _build_resident_context(resident_id, days=30)
    context_text = _context_to_text(ctx, 30)

    prompt = f"""Revisa el PAI actual de este residente comparando objetivos con datos reales.

PAI ACTUAL:
{plan.html_content}

DATOS RECIENTES (últimos 30 días):
{context_text}"""

    try:
        review_html = _call_claude(PAI_REVIEW_SYSTEM, prompt)
        review_html = review_html.strip()
        if review_html.startswith('```'):
            review_html = review_html.split('\n', 1)[1] if '\n' in review_html else review_html[3:]
        if review_html.endswith('```'):
            review_html = review_html.rsplit('```', 1)[0]
        review_html = review_html.strip()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    plan.ai_review = review_html
    plan.ai_review_date = datetime.now()
    log_audit('update', 'care_plan', plan.id,
              {'resident_id': resident_id, 'campo': 'ai_review'})
    ok, error = _safe_commit('Error al guardar la revision del PAI')
    if not ok:
        return jsonify({'error': error}), 500

    return jsonify({
        'html': review_html,
        'plan_id': plan.id,
        'review_date': datetime.now().strftime('%d/%m/%Y %H:%M'),
    })


# ── Auto PAI Review ────────────────────────────────────────────────────────

PAI_AUTO_REVIEW_SYSTEM = """Eres un profesional sociosanitario de la residencia La Vila Gran.
Se ha detectado un EVENTO SIGNIFICATIVO en un residente que tiene un PAI activo.
Tu tarea es revisar si el PAI necesita actualizaciones en base al evento.

Analiza:
1. Si algún objetivo del PAI se ve afectado por el evento
2. Si hacen falta nuevas intervenciones
3. Si algún indicador ha cambiado significativamente

FORMATO: HTML breve (sin <html>/<body>).
- Empieza con <strong>Evento detectado:</strong> y descripcion del evento
- Luego <strong>Impacto en el PAI:</strong> con lista de cambios sugeridos
- Usa colores: rojo para cambios urgentes, naranja para ajustes recomendados
- Si el PAI no necesita cambios, indícalo claramente
Escribe en espanol. No inventes datos."""


def auto_review_care_plan(resident_id, event_description):
    """Auto-review PAI when a significant event occurs. Creates notification if changes suggested."""
    plan = CarePlan.query.filter_by(
        resident_id=resident_id, status='active',
    ).order_by(CarePlan.created_at.desc()).first()

    if not plan or not plan.html_content:
        return  # No active PAI, nothing to review

    # Throttle: don't review if already reviewed in last 24h
    if plan.ai_review_date:
        since_last = (datetime.now() - plan.ai_review_date).total_seconds()
        if since_last < 24 * 3600:
            return

    ctx = _build_resident_context(resident_id, days=14)
    if not ctx:
        return
    context_text = _context_to_text(ctx, 14)

    prompt = f"""EVENTO: {event_description}

PAI ACTUAL:
{plan.html_content[:3000]}

DATOS RECIENTES (14 días):
{context_text}"""

    try:
        review_html = _call_claude(PAI_AUTO_REVIEW_SYSTEM, prompt)
        review_html = review_html.strip()
        if review_html.startswith('```'):
            review_html = review_html.split('\n', 1)[1] if '\n' in review_html else review_html[3:]
        if review_html.endswith('```'):
            review_html = review_html.rsplit('```', 1)[0]
        review_html = review_html.strip()
    except Exception:
        return

    # Update PAI with review
    plan.ai_review = review_html
    plan.ai_review_date = datetime.now()

    # Create notification for admin
    resident = db.session.get(Resident, resident_id)
    r_name = resident.name if resident else 'Residente'
    db.session.add(Notification(
        type='pai_review',
        title=f'Revisión automática PAI: {r_name}',
        message=review_html,
        severity='warning',
        resident_id=resident_id,
        link=f'/admin/resident/{resident_id}',
    ))
    log_audit('update', 'care_plan', plan.id,
              {'resident_id': resident_id, 'campo': 'ai_review', 'origen': 'automatico'})
    ok, error = _safe_commit('Error al guardar la revision automatica del PAI')
    if not ok:
        app.logger.error('No se pudo guardar la revision automatica del PAI: %s', error)


# ── Predictive Risk Watchlist ────────────────────────────────────────────────

WATCHLIST_SYSTEM = """Eres un profesional sociosanitario de la residencia La Vila Gran.
Analiza los datos de todos los residentes y genera una LISTA DE VIGILANCIA priorizada.
Para cada residente con riesgo, indica:
- Nombre y habitacion
- Nivel de riesgo: ALTO (rojo), MEDIO (naranja), BAJO (amarillo)
- Motivos concretos basados en los datos (no inventes)
- Acciones recomendadas

FORMATO: HTML con una tabla ordenada por riesgo (alto primero).
Columnas: Residente | Riesgo | Motivos | Acciones recomendadas
Usa colores: <span style="color:red">ALTO</span>, <span style="color:orange">MEDIO</span>, <span style="color:#cc0">BAJO</span>
Si un residente no tiene riesgo destacable, NO lo incluyas.
Maximo 20 residentes. Escribe en espanol. Sin emojis."""


@bp.route('/api/ai/risk-watchlist', methods=['POST'])
@admin_required
def ai_risk_watchlist():
    """AI-generated weekly risk watchlist for all residents."""
    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()

    lines = [f"WATCHLIST DE RIESGO — {datetime.now().strftime('%d/%m/%Y')} — {len(residents)} residentes activos\n"]

    for r in residents:
        ctx = _build_resident_context(r.id, days=30)
        if not ctx:
            continue

        # Build compact summary per resident
        summary_parts = [f"\n--- {r.name} (Hab.{r.room_number or '?'}, dep: {ctx['profile']['dependency']}) ---"]

        if ctx['assessments']:
            for a in ctx['assessments']:
                summary_parts.append(f"  Valoración: {a['scale']} {a['score']} ({a['interpretation']}) [{a['date']}]")

        if ctx['incidents']:
            falls = [i for i in ctx['incidents'] if 'caida' in i['title'].lower() or 'caiguda' in i['title'].lower()]
            summary_parts.append(f"  Incidencias: {len(ctx['incidents'])} (caídas: {len(falls)})")

        if ctx.get('mood'):
            avg_mood = sum(m['score'] for m in ctx['mood']) / len(ctx['mood'])
            summary_parts.append(f"  Ánimo medio: {avg_mood:.1f}/5 ({len(ctx['mood'])} registros)")

        if ctx['vital_signs']:
            out_of_range = [v for v in ctx['vital_signs'] if 'fuera' in str(v).lower()]
            summary_parts.append(f"  Constantes: {len(ctx['vital_signs'])} lecturas")

        if ctx['care_records']:
            summary_parts.append(f"  Atenciones: {len(ctx['care_records'])}")

        if ctx.get('activities'):
            participated = sum(1 for a in ctx['activities'] if 'participat' in a.get('engagement', '').lower())
            summary_parts.append(f"  Actividades: {participated}/{len(ctx['activities'])} participadas")

        lines.extend(summary_parts)

    prompt = "Genera una lista de vigilancia basada en estos datos:\n" + '\n'.join(lines)

    try:
        html = _call_claude(WATCHLIST_SYSTEM, prompt)
        html = html.strip()
        if html.startswith('```'):
            html = html.split('\n', 1)[1] if '\n' in html else html[3:]
        if html.endswith('```'):
            html = html.rsplit('```', 1)[0]
        return jsonify({'html': html.strip(), 'generated_at': datetime.now().strftime('%d/%m/%Y %H:%M')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
