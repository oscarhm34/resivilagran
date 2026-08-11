"""Clinical assessment scales (Barthel, Norton), weight tracking, and meal intake."""
from __future__ import annotations
import json as _json
from datetime import datetime, timedelta, date

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_login import current_user
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import joinedload

from .. import db
from ..models import (
    Resident, Cleaner, AssessmentRecord, WeightRecord, MealIntakeRecord, Notification,
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

MEAL_TYPES = ['desayuno', 'comida', 'merienda', 'cena']
MEAL_LABELS = {'desayuno': 'Desayuno', 'comida': 'Comida', 'merienda': 'Merienda', 'cena': 'Cena'}


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


# ── Weight tracking ──────────────────────────────────────────────────────────

@bp.route('/admin/resident/<int:resident_id>/weight', methods=['POST'])
@admin_required
def record_weight(resident_id: int):
    resident = db.session.get(Resident, resident_id)
    if not resident:
        return jsonify({'error': 'Residente no encontrado'}), 404

    weight = request.form.get('weight_kg', type=float)
    if not weight or weight <= 0:
        flash('Peso no valido.', 'danger')
        return redirect(url_for('residents.resident_detail', resident_id=resident_id))

    record = WeightRecord(
        resident_id=resident_id, weight_kg=weight,
        recorded_by=current_user.id,
        notes=request.form.get('notes', '').strip() or None,
    )
    db.session.add(record)

    # Check for significant weight loss
    _check_weight_loss(resident_id, weight, resident.name)

    db.session.commit()
    flash(f'Peso registrado: {weight} kg', 'success')
    return redirect(url_for('residents.resident_detail', resident_id=resident_id))


def _check_weight_loss(resident_id: int, current_weight: float, resident_name: str):
    """Generate notification if significant weight loss detected."""
    now = datetime.now()

    # Check 30-day loss (>5%)
    cutoff_30 = now - timedelta(days=30)
    prev_30 = WeightRecord.query.filter(
        WeightRecord.resident_id == resident_id,
        WeightRecord.recorded_at >= cutoff_30,
    ).order_by(WeightRecord.recorded_at.asc()).first()

    if prev_30 and prev_30.weight_kg > 0:
        loss_pct = ((prev_30.weight_kg - current_weight) / prev_30.weight_kg) * 100
        if loss_pct >= 5:
            db.session.add(Notification(
                type='weight_alert',
                title=f'Perdida de peso: {resident_name} ha perdido {loss_pct:.1f}% en 30 dias ({prev_30.weight_kg:.1f} → {current_weight:.1f} kg)',
                severity='warning', resident_id=resident_id,
                link=f'/admin/resident/{resident_id}',
            ))
            return

    # Check 180-day loss (>10%)
    cutoff_180 = now - timedelta(days=180)
    prev_180 = WeightRecord.query.filter(
        WeightRecord.resident_id == resident_id,
        WeightRecord.recorded_at >= cutoff_180,
    ).order_by(WeightRecord.recorded_at.asc()).first()

    if prev_180 and prev_180.weight_kg > 0:
        loss_pct = ((prev_180.weight_kg - current_weight) / prev_180.weight_kg) * 100
        if loss_pct >= 10:
            db.session.add(Notification(
                type='weight_alert',
                title=f'Perdida de peso grave: {resident_name} ha perdido {loss_pct:.1f}% en 6 meses ({prev_180.weight_kg:.1f} → {current_weight:.1f} kg)',
                severity='critical', resident_id=resident_id,
                link=f'/admin/resident/{resident_id}',
            ))


# ── Meal intake (admin) ──────────────────────────────────────────────────────

@bp.route('/admin/resident/<int:resident_id>/meal-intake', methods=['POST'])
@admin_required
def record_meal_admin(resident_id: int):
    data = request.get_json() if request.is_json else None
    if data:
        meal_date = date.fromisoformat(data['date'])
        meal_type = data['meal_type']
        intake_pct = data['intake_pct']
        fluid_ml = data.get('fluid_ml')
        notes = data.get('notes', '').strip() or None
    else:
        meal_date = date.fromisoformat(request.form['date'])
        meal_type = request.form['meal_type']
        intake_pct = int(request.form['intake_pct'])
        fluid_ml = request.form.get('fluid_ml', type=int)
        notes = request.form.get('notes', '').strip() or None

    if meal_type not in MEAL_TYPES:
        return jsonify({'error': 'Tipo de comida no valido'}), 400

    existing = MealIntakeRecord.query.filter_by(
        resident_id=resident_id, date=meal_date, meal_type=meal_type,
    ).first()

    if existing:
        existing.intake_pct = intake_pct
        existing.fluid_ml = fluid_ml
        existing.notes = notes
        existing.recorded_by = current_user.id
        existing.recorded_at = datetime.now()
    else:
        db.session.add(MealIntakeRecord(
            resident_id=resident_id, date=meal_date, meal_type=meal_type,
            intake_pct=intake_pct, fluid_ml=fluid_ml, notes=notes,
            recorded_by=current_user.id,
        ))

    db.session.commit()
    return jsonify({'ok': True})


# ── Meal intake (worker API) ─────────────────────────────────────────────────

@bp.route('/api/resident/<int:resident_id>/meal-intake', methods=['POST'])
@jwt_required()
def record_meal_worker(resident_id: int):
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autorizado'}), 403

    data = request.get_json()
    meal_date = date.fromisoformat(data['date'])
    meal_type = data['meal_type']
    intake_pct = data['intake_pct']
    fluid_ml = data.get('fluid_ml')

    if meal_type not in MEAL_TYPES:
        return jsonify({'error': 'Tipo no valido'}), 400

    existing = MealIntakeRecord.query.filter_by(
        resident_id=resident_id, date=meal_date, meal_type=meal_type,
    ).first()

    if existing:
        existing.intake_pct = intake_pct
        existing.fluid_ml = fluid_ml
        existing.recorded_by = worker.id
        existing.recorded_at = datetime.now()
    else:
        db.session.add(MealIntakeRecord(
            resident_id=resident_id, date=meal_date, meal_type=meal_type,
            intake_pct=intake_pct, fluid_ml=fluid_ml,
            recorded_by=worker.id,
        ))

    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/resident/<int:resident_id>/today-meals')
@jwt_required()
def today_meals(resident_id: int):
    today = date.today()
    records = MealIntakeRecord.query.filter_by(
        resident_id=resident_id, date=today,
    ).all()
    meals = {r.meal_type: {'intake_pct': r.intake_pct, 'fluid_ml': r.fluid_ml} for r in records}
    return jsonify({'date': today.isoformat(), 'meals': meals})


# ── Weight (worker API) ──────────────────────────────────────────────────────

@bp.route('/api/resident/<int:resident_id>/weight', methods=['POST'])
@jwt_required()
def record_weight_worker(resident_id: int):
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autorizado'}), 403

    data = request.get_json()
    weight = data.get('weight_kg', type=float) if isinstance(data.get('weight_kg'), (int, float)) else None
    try:
        weight = float(data.get('weight_kg'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Peso no valido'}), 400

    if weight <= 0:
        return jsonify({'error': 'Peso no valido'}), 400

    resident = db.session.get(Resident, resident_id)
    if not resident:
        return jsonify({'error': 'Residente no encontrado'}), 404

    db.session.add(WeightRecord(
        resident_id=resident_id, weight_kg=weight, recorded_by=worker.id,
    ))
    _check_weight_loss(resident_id, weight, resident.name)
    db.session.commit()
    return jsonify({'ok': True, 'weight_kg': weight})


# ── Assessment history data for resident detail ──────────────────────────────

def get_resident_assessment_data(resident_id: int) -> dict:
    """Fetch assessment data for the resident detail page."""
    assessments = AssessmentRecord.query.filter_by(
        resident_id=resident_id,
    ).order_by(AssessmentRecord.assessed_at.desc()).all()

    barthel_records = [a for a in assessments if a.scale_type == 'barthel']
    norton_records = [a for a in assessments if a.scale_type == 'norton']

    weights = WeightRecord.query.filter_by(
        resident_id=resident_id,
    ).order_by(WeightRecord.recorded_at.desc()).limit(50).all()

    today = date.today()
    today_meals = MealIntakeRecord.query.filter_by(
        resident_id=resident_id, date=today,
    ).all()
    today_meals_map = {m.meal_type: m for m in today_meals}

    # Last 7 days meals
    week_ago = today - timedelta(days=6)
    week_meals = MealIntakeRecord.query.filter(
        MealIntakeRecord.resident_id == resident_id,
        MealIntakeRecord.date >= week_ago,
    ).order_by(MealIntakeRecord.date.desc(), MealIntakeRecord.meal_type).all()

    return {
        'barthel_records': barthel_records,
        'norton_records': norton_records,
        'weights': weights,
        'today_meals': today_meals_map,
        'week_meals': week_meals,
        'meal_types': MEAL_TYPES,
        'meal_labels': MEAL_LABELS,
    }
