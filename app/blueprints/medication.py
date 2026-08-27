"""Medication management — prescriptions and administration tracking."""
from __future__ import annotations
from datetime import datetime, timedelta, date

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_login import current_user
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import joinedload

from .. import db
from ..models import (
    Resident, Cleaner, MedicationPrescription, MedicationAdministration, Notification,
)
from ..utils import admin_required, _safe_commit, _safe_flush, log_audit

bp = Blueprint('medication', __name__)

ROUTES = ['oral', 'sublingual', 'topica', 'inhalatoria', 'rectal', 'subcutanea',
          'intramuscular', 'intravenosa', 'oftalmica', 'otica', 'nasal', 'transdermica']
ROUTE_LABELS = {
    'oral': 'Oral', 'sublingual': 'Sublingual', 'topica': 'Topica',
    'inhalatoria': 'Inhalatoria', 'rectal': 'Rectal', 'subcutanea': 'Subcutanea',
    'intramuscular': 'Intramuscular', 'intravenosa': 'Intravenosa',
    'oftalmica': 'Oftalmica', 'otica': 'Otica', 'nasal': 'Nasal',
    'transdermica': 'Transdermica',
}
STATUS_LABELS = {
    'given': 'Administrado', 'refused': 'Rechazado', 'omitted': 'Omitido',
    'not_available': 'No disponible',
}


# ── Admin: Medication Dashboard ──────────────────────────────────────────────

@bp.route('/admin/medication')
@admin_required
def admin_medication():
    """Medication overview: today's pending and completed administrations."""
    today = date.today()
    now = datetime.now()
    current_hour = now.strftime('%H:%M')

    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()

    # Get all active prescriptions with today's administrations
    prescriptions = MedicationPrescription.query.options(
        joinedload(MedicationPrescription.resident),
    ).filter_by(active=True).order_by(
        MedicationPrescription.resident_id,
    ).all()

    # Today's administrations
    day_start = datetime.combine(today, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    today_admins = MedicationAdministration.query.options(
        joinedload(MedicationAdministration.worker),
    ).filter(
        MedicationAdministration.administered_at >= day_start,
        MedicationAdministration.administered_at < day_end,
    ).all()
    admin_by_presc = {}
    for a in today_admins:
        admin_by_presc.setdefault(a.prescription_id, []).append(a)

    # Build per-resident medication view
    resident_meds = {}
    for p in prescriptions:
        if p.end_date and p.end_date < today:
            continue
        if p.start_date and p.start_date > today:
            continue

        times = [t.strip() for t in (p.schedule_times or '').split(',') if t.strip()]
        admins_today = admin_by_presc.get(p.id, [])
        admin_times = {a.scheduled_time for a in admins_today}

        med_info = {
            'prescription': p,
            'times': times,
            'administrations': admins_today,
            'pending_times': [t for t in times if t not in admin_times and t <= current_hour],
            'upcoming_times': [t for t in times if t not in admin_times and t > current_hour],
            'completed_times': [t for t in times if t in admin_times],
        }

        resident_meds.setdefault(p.resident_id, {
            'resident': p.resident,
            'medications': [],
            'total_pending': 0,
            'total_completed': 0,
        })
        resident_meds[p.resident_id]['medications'].append(med_info)
        resident_meds[p.resident_id]['total_pending'] += len(med_info['pending_times'])
        resident_meds[p.resident_id]['total_completed'] += len(med_info['completed_times'])

    # Sort: residents with pending meds first
    sorted_residents = sorted(resident_meds.values(), key=lambda r: (-r['total_pending'], r['resident'].name))

    total_pending = sum(r['total_pending'] for r in sorted_residents)
    total_completed = sum(r['total_completed'] for r in sorted_residents)

    return render_template('admin_medication.html',
        resident_meds=sorted_residents,
        total_pending=total_pending,
        total_completed=total_completed,
        total_residents_with_meds=len(sorted_residents),
        residents=residents,
        routes=ROUTES, route_labels=ROUTE_LABELS,
        today=today,
    )


# ── Admin: Prescription CRUD ─────────────────────────────────────────────────

@bp.route('/admin/medication/prescriptions/<int:resident_id>')
@admin_required
def resident_prescriptions(resident_id: int):
    """View/manage prescriptions for a resident."""
    resident = db.session.get(Resident, resident_id)
    if not resident:
        flash('Residente no encontrado.', 'danger')
        return redirect(url_for('medication.admin_medication'))

    prescriptions = MedicationPrescription.query.filter_by(
        resident_id=resident_id,
    ).order_by(MedicationPrescription.active.desc(), MedicationPrescription.drug_name).all()

    return render_template('admin_medication_prescriptions.html',
        resident=resident, prescriptions=prescriptions,
        routes=ROUTES, route_labels=ROUTE_LABELS,
    )


@bp.route('/admin/medication/prescriptions/save', methods=['POST'])
@admin_required
def save_prescription():
    """Create or update a prescription."""
    presc_id = request.form.get('prescription_id', type=int)
    resident_id = request.form.get('resident_id', type=int)

    if presc_id:
        p = db.session.get(MedicationPrescription, presc_id)
        if not p:
            flash('Prescripcion no encontrada.', 'danger')
            return redirect(url_for('medication.admin_medication'))
    else:
        p = MedicationPrescription(resident_id=resident_id, created_by=current_user.id)
        db.session.add(p)

    p.drug_name = request.form.get('drug_name', '').strip()
    p.dose = request.form.get('dose', '').strip()
    p.route = request.form.get('route', 'oral')
    p.frequency = request.form.get('frequency', '').strip()
    p.schedule_times = request.form.get('schedule_times', '').strip()
    p.instructions = request.form.get('instructions', '').strip() or None
    p.prescribed_by = request.form.get('prescribed_by', '').strip() or None
    p.barcode = request.form.get('barcode', '').strip() or None
    p.active = bool(request.form.get('active'))

    start = request.form.get('start_date', '').strip()
    end = request.form.get('end_date', '').strip()
    p.start_date = date.fromisoformat(start) if start else None
    p.end_date = date.fromisoformat(end) if end else None

    ok, error = _safe_flush('Error al guardar la prescripcion')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('medication.admin_medication'))
    log_audit('update' if presc_id else 'create', 'medication_prescription', p.id,
              {'drug': p.drug_name, 'dose': p.dose, 'resident_id': p.resident_id})
    ok, error = _safe_commit('Error al guardar la prescripcion')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('medication.admin_medication'))

    flash(f'Prescripcion {"actualizada" if presc_id else "creada"}: {p.drug_name}', 'success')
    return redirect(url_for('medication.resident_prescriptions', resident_id=p.resident_id))


@bp.route('/admin/medication/prescriptions/<int:presc_id>/toggle', methods=['POST'])
@admin_required
def toggle_prescription(presc_id: int):
    p = db.session.get(MedicationPrescription, presc_id)
    if p:
        p.active = not p.active
        log_audit('update', 'medication_prescription', p.id,
                  {'activa': p.active, 'resident_id': p.resident_id})
        ok, error = _safe_commit('Error al cambiar el estado de la prescripcion')
        if not ok:
            return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'active': p.active if p else False})


@bp.route('/admin/medication/prescriptions/<int:presc_id>/delete', methods=['POST'])
@admin_required
def delete_prescription(presc_id: int):
    p = db.session.get(MedicationPrescription, presc_id)
    if not p:
        flash('Prescripcion no encontrada.', 'danger')
        return redirect(url_for('medication.admin_medication'))
    resident_id = p.resident_id
    MedicationAdministration.query.filter_by(prescription_id=presc_id).delete()
    log_audit('delete', 'medication_prescription', presc_id,
              {'drug': p.drug_name, 'resident_id': resident_id})
    db.session.delete(p)
    ok, error = _safe_commit('Error al eliminar la prescripcion')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('medication.resident_prescriptions', resident_id=resident_id))
    flash('Prescripcion eliminada.', 'success')
    return redirect(url_for('medication.resident_prescriptions', resident_id=resident_id))


# ── Admin: Record Administration ─────────────────────────────────────────────

@bp.route('/admin/medication/administer', methods=['POST'])
@admin_required
def administer_medication():
    """Record a medication administration from admin panel."""
    data = request.get_json() if request.is_json else None
    if data:
        presc_id = data['prescription_id']
        scheduled_time = data.get('scheduled_time', '')
        status = data.get('status', 'given')
        notes = data.get('notes', '').strip() or None
    else:
        presc_id = request.form.get('prescription_id', type=int)
        scheduled_time = request.form.get('scheduled_time', '')
        status = request.form.get('status', 'given')
        notes = request.form.get('notes', '').strip() or None

    p = db.session.get(MedicationPrescription, presc_id)
    if not p:
        return jsonify({'error': 'Prescripcion no encontrada'}), 404

    admin_rec = MedicationAdministration(
        prescription_id=presc_id,
        administered_by=current_user.id,
        scheduled_time=scheduled_time,
        status=status,
        notes=notes,
    )
    db.session.add(admin_rec)

    # Generate notification if refused or omitted
    if status in ('refused', 'omitted', 'not_available'):
        resident_name = p.resident.name if p.resident else 'Residente'
        status_label = STATUS_LABELS.get(status, status)
        db.session.add(Notification(
            type='medication_alert',
            title=f'{p.drug_name} {status_label}: {resident_name}',
            message=notes,
            severity='warning',
            resident_id=p.resident_id,
            link='/admin/medication',
        ))

    ok, error = _safe_flush('Error al registrar la administracion')
    if not ok:
        return jsonify({'error': error}), 500
    log_audit('create', 'medication_administration', admin_rec.id,
              {'prescription_id': presc_id, 'estado': status,
               'resident_id': p.resident_id})
    ok, error = _safe_commit('Error al registrar la administracion')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True})


# ── Worker API: Medication ────────────────────────────────────────────────────

@bp.route('/api/resident/<int:resident_id>/medications')
@jwt_required()
def worker_resident_medications(resident_id: int):
    """Get active medications for a resident (worker view)."""
    today = date.today()
    now = datetime.now()

    prescriptions = MedicationPrescription.query.filter_by(
        resident_id=resident_id, active=True,
    ).order_by(MedicationPrescription.drug_name).all()

    day_start = datetime.combine(today, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    today_admins = MedicationAdministration.query.filter(
        MedicationAdministration.prescription_id.in_([p.id for p in prescriptions]),
        MedicationAdministration.administered_at >= day_start,
        MedicationAdministration.administered_at < day_end,
    ).all()
    admin_map = {}
    for a in today_admins:
        admin_map.setdefault(a.prescription_id, set()).add(a.scheduled_time)

    meds = []
    for p in prescriptions:
        if p.end_date and p.end_date < today:
            continue
        if p.start_date and p.start_date > today:
            continue
        times = [t.strip() for t in (p.schedule_times or '').split(',') if t.strip()]
        admin_times = admin_map.get(p.id, set())
        meds.append({
            'id': p.id,
            'drug_name': p.drug_name,
            'dose': p.dose,
            'route': ROUTE_LABELS.get(p.route, p.route),
            'frequency': p.frequency,
            'instructions': p.instructions or '',
            'barcode': p.barcode or '',
            'times': times,
            'administered': [t for t in times if t in admin_times],
            'pending': [t for t in times if t not in admin_times],
        })

    return jsonify({'medications': meds})


@bp.route('/api/medication/administer', methods=['POST'])
@jwt_required()
def worker_administer():
    """Record medication administration from worker app."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autorizado'}), 403

    data = request.get_json()
    presc_id = data.get('prescription_id')
    scheduled_time = data.get('scheduled_time', '')
    status = data.get('status', 'given')
    notes = data.get('notes', '').strip() or None

    p = db.session.get(MedicationPrescription, presc_id)
    if not p:
        return jsonify({'error': 'Prescripcion no encontrada'}), 404

    admin_rec = MedicationAdministration(
        prescription_id=presc_id,
        administered_by=worker.id,
        scheduled_time=scheduled_time,
        status=status,
        notes=notes,
    )
    db.session.add(admin_rec)

    if status in ('refused', 'omitted', 'not_available'):
        resident_name = p.resident.name if p.resident else 'Residente'
        status_label = STATUS_LABELS.get(status, status)
        db.session.add(Notification(
            type='medication_alert',
            title=f'{p.drug_name} {status_label}: {resident_name}',
            message=notes, severity='warning',
            resident_id=p.resident_id, link='/admin/medication',
        ))

    ok, error = _safe_flush('Error al registrar la administracion')
    if not ok:
        return jsonify({'error': error}), 500
    log_audit('create', 'medication_administration', admin_rec.id,
              {'prescription_id': presc_id, 'estado': status,
               'resident_id': p.resident_id, 'cleaner_id': worker.id})
    ok, error = _safe_commit('Error al registrar la administracion')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True})


# ── Barcode Verification ──────────────────────────────────────────────────────

@bp.route('/api/medication/verify-barcode', methods=['POST'])
@jwt_required()
def verify_barcode():
    """Verify a scanned barcode against a prescription."""
    data = request.get_json(silent=True) or {}
    scanned_code = (data.get('barcode') or '').strip()
    prescription_id = data.get('prescription_id')
    resident_id = data.get('resident_id')

    if not scanned_code:
        return jsonify({'error': 'Código de barras vacío'}), 400

    # If prescription_id given, verify against that specific prescription
    if prescription_id:
        p = db.session.get(MedicationPrescription, prescription_id)
        if not p:
            return jsonify({'match': False, 'error': 'Prescripción no encontrada'}), 404
        if not p.barcode:
            return jsonify({'match': False, 'message': 'Prescripción sin barcode registrado', 'drug_name': p.drug_name})
        if p.barcode.strip() == scanned_code:
            return jsonify({'match': True, 'drug_name': p.drug_name, 'dose': p.dose,
                            'resident_name': p.resident.name if p.resident else ''})
        else:
            return jsonify({'match': False, 'expected': p.drug_name,
                            'message': f'Barcode no coincide con {p.drug_name}'})

    # If resident_id given, search all active prescriptions for this resident
    if resident_id:
        today = date.today()
        prescriptions = MedicationPrescription.query.filter_by(
            resident_id=resident_id, active=True,
        ).all()
        for p in prescriptions:
            if p.end_date and p.end_date < today:
                continue
            if p.barcode and p.barcode.strip() == scanned_code:
                return jsonify({
                    'match': True, 'prescription_id': p.id,
                    'drug_name': p.drug_name, 'dose': p.dose,
                    'resident_name': p.resident.name if p.resident else '',
                })
        return jsonify({'match': False, 'message': 'Barcode no coincide con ningún medicamento activo del residente'})

    # Global search across all active prescriptions
    match = MedicationPrescription.query.filter_by(barcode=scanned_code, active=True).first()
    if match:
        return jsonify({
            'match': True, 'prescription_id': match.id,
            'drug_name': match.drug_name, 'dose': match.dose,
            'resident_id': match.resident_id,
            'resident_name': match.resident.name if match.resident else '',
        })

    return jsonify({'match': False, 'message': 'Barcode no reconocido'})


# ── AI Medication Interaction Checker ──────────────────────────────────────────

@bp.route('/api/medication/interaction-check', methods=['POST'])
@admin_required
def check_interactions():
    """Use Claude to check for potential drug interactions."""
    from ..blueprints.assessments import _call_claude

    data = request.get_json() or {}
    resident_id = data.get('resident_id')
    new_drug = data.get('drug_name', '').strip()
    new_dose = data.get('dose', '').strip()

    if not resident_id or not new_drug:
        return jsonify({'error': 'Faltan datos'}), 400

    resident = db.session.get(Resident, resident_id)
    if not resident:
        return jsonify({'error': 'Residente no encontrado'}), 404

    # Get all active prescriptions
    prescriptions = MedicationPrescription.query.filter_by(
        resident_id=resident_id, active=True).all()

    if not prescriptions:
        return jsonify({'interactions': [], 'message': 'No hay medicacion activa previa.'})

    med_list = '\n'.join(
        f"- {p.drug_name} {p.dose} ({p.route}, {p.frequency})"
        for p in prescriptions
    )

    system = (
        "Eres un asistente farmacologico informativo. "
        "Analiza posibles interacciones entre los medicamentos listados y el nuevo farmaco. "
        "IMPORTANTE: Esta informacion es solo orientativa. No prescribes ni contraindicas. "
        "Solo informas al profesional sanitario de posibles interacciones conocidas. "
        "La decision clinica es siempre del profesional. "
        "Responde en espanol. Si no hay interacciones relevantes, indicalo claramente. "
        "Formato: lista breve de interacciones detectadas con nivel de gravedad (leve/moderada/grave). "
        "Si no encuentras interacciones, responde con un mensaje tranquilizador."
    )

    prompt = (
        f"Residente: {resident.name}, {resident.diagnoses or 'sin diagnosticos registrados'}, "
        f"alergias: {resident.allergies or 'ninguna registrada'}\n\n"
        f"Medicacion activa actual:\n{med_list}\n\n"
        f"NUEVO FARMACO a anadir: {new_drug} {new_dose}\n\n"
        f"Analiza posibles interacciones del nuevo farmaco con la medicacion actual."
    )

    try:
        response = _call_claude(system, prompt)
        return jsonify({'analysis': response, 'drug': new_drug})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
