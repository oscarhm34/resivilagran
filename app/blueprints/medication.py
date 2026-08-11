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
from ..utils import admin_required

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
    p.active = bool(request.form.get('active'))

    start = request.form.get('start_date', '').strip()
    end = request.form.get('end_date', '').strip()
    p.start_date = date.fromisoformat(start) if start else None
    p.end_date = date.fromisoformat(end) if end else None

    db.session.commit()
    flash(f'Prescripcion {"actualizada" if presc_id else "creada"}: {p.drug_name}', 'success')
    return redirect(url_for('medication.resident_prescriptions', resident_id=p.resident_id))


@bp.route('/admin/medication/prescriptions/<int:presc_id>/toggle', methods=['POST'])
@admin_required
def toggle_prescription(presc_id: int):
    p = db.session.get(MedicationPrescription, presc_id)
    if p:
        p.active = not p.active
        db.session.commit()
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
    db.session.delete(p)
    db.session.commit()
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
            link=f'/admin/medication',
        ))

    db.session.commit()
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

    db.session.commit()
    return jsonify({'ok': True})
