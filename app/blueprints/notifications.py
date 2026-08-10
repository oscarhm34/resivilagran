"""Notification center: in-panel notifications for the admin dashboard."""
from __future__ import annotations

from flask import Blueprint, request, jsonify, render_template
from flask_login import current_user, login_required
from datetime import datetime, timedelta, date

from .. import db
from ..models import (Notification, VitalSignReading, VitalSignType,
                      CareRecord, CleaningRecord, Resident, Cleaner,
                      Incident, LegalDocument, DocumentSignature,
                      ShiftAssignment, ShiftCoverageRequirement, ShiftType)
from ..utils import admin_required

bp = Blueprint('notifications', __name__)


# ── Helper: duplicate check ────────────────────────────────────────────────

def _notif_exists(notif_type: str, title: str, hours: int = 24) -> bool:
    """Return True if a notification with same type+title exists in the last N hours."""
    cutoff = datetime.now() - timedelta(hours=hours)
    return db.session.query(
        Notification.query.filter(
            Notification.type == notif_type,
            Notification.title == title,
            Notification.created_at >= cutoff,
        ).exists()
    ).scalar()


# ── Core: generate notifications ───────────────────────────────────────────

def _generate_notifications() -> int:
    """Scan for events and create Notification records. Returns count of new notifications."""
    created = 0
    now = datetime.now()
    today_start = datetime.combine(now.date(), datetime.min.time())
    today_end = today_start + timedelta(days=1)

    # 1. vital_alert — out-of-range vital signs recorded today
    readings = (
        db.session.query(VitalSignReading, VitalSignType)
        .join(VitalSignType, VitalSignReading.vital_sign_type_id == VitalSignType.id)
        .filter(VitalSignReading.recorded_at >= today_start,
                VitalSignReading.recorded_at < today_end)
        .all()
    )
    for reading, vtype in readings:
        out_of_range = False
        if vtype.min_value is not None and reading.value < vtype.min_value:
            out_of_range = True
        if vtype.max_value is not None and reading.value > vtype.max_value:
            out_of_range = True
        if not out_of_range:
            continue
        # Get resident name through care_record
        care_rec = reading.care_record
        resident_name = care_rec.resident.name if care_rec and care_rec.resident else 'Desconocido'
        resident_id = care_rec.resident_id if care_rec else None
        min_str = str(vtype.min_value) if vtype.min_value is not None else '—'
        max_str = str(vtype.max_value) if vtype.max_value is not None else '—'
        title = f"Alerta vital: {resident_name} - {vtype.name}: {reading.value} {vtype.unit} (rango: {min_str}-{max_str})"
        if not _notif_exists('vital_alert', title):
            db.session.add(Notification(
                type='vital_alert', title=title, severity='critical',
                resident_id=resident_id,
                link='/admin/residents',
            ))
            created += 1

    # 2. incident — open incidents with high/critical severity in last 24h
    cutoff_24h = now - timedelta(hours=24)
    incidents = Incident.query.filter(
        Incident.status == 'open',
        Incident.severity.in_(['critical', 'high']),
        Incident.created_at >= cutoff_24h,
    ).all()
    for inc in incidents:
        sev_label = 'Critica' if inc.severity == 'critical' else 'Alta'
        title = f"Incidencia {sev_label}: {inc.title}"
        if not _notif_exists('incident', title):
            db.session.add(Notification(
                type='incident', title=title, severity='warning',
                resident_id=inc.resident_id,
                link=f'/admin/incidents?status=open',
            ))
            created += 1

    # 3. document_pending — active legal docs with pending signatures
    total_workers = Cleaner.query.filter_by(active=True).count()
    active_docs = LegalDocument.query.filter_by(active=True).all()
    pending_count = 0
    for doc in active_docs:
        signed = DocumentSignature.query.filter_by(document_id=doc.id).count()
        if total_workers - signed > 0:
            pending_count += 1
    if pending_count > 0:
        title = f"Hay {pending_count} documento{'s' if pending_count != 1 else ''} pendiente{'s' if pending_count != 1 else ''} de firma"
        if not _notif_exists('document_pending', title):
            db.session.add(Notification(
                type='document_pending', title=title, severity='info',
                link='/admin/documents',
            ))
            created += 1

    # 4. stale_session — sessions open for more than 24h
    stale_cleaning = CleaningRecord.query.filter(
        CleaningRecord.end_time.is_(None),
        CleaningRecord.start_time < cutoff_24h,
    ).count()
    stale_care = CareRecord.query.filter(
        CareRecord.end_time.is_(None),
        CareRecord.start_time < cutoff_24h,
    ).count()
    stale_total = stale_cleaning + stale_care
    if stale_total > 0:
        title = f"{stale_total} sesion{'es' if stale_total != 1 else ''} lleva{'n' if stale_total != 1 else ''} mas de 24h abierta{'s' if stale_total != 1 else ''}"
        if not _notif_exists('stale_session', title):
            db.session.add(Notification(
                type='stale_session', title=title, severity='warning',
                link='/',
            ))
            created += 1

    # 5. coverage_gap — tomorrow's shift coverage
    tomorrow = now.date() + timedelta(days=1)
    requirements = ShiftCoverageRequirement.query.all()
    is_weekend = tomorrow.weekday() >= 5
    for req in requirements:
        if req.day_type == 'weekday' and is_weekend:
            continue
        if req.day_type == 'weekend' and not is_weekend:
            continue
        actual = ShiftAssignment.query.filter(
            ShiftAssignment.date == tomorrow,
            ShiftAssignment.shift_type_id == req.shift_type_id,
        ).count()
        needed = req.min_workers
        if actual < needed:
            shift_name = req.shift_type.name if req.shift_type else f'ID {req.shift_type_id}'
            title = f"Falta cobertura manana: turno {shift_name} tiene {actual}/{needed} trabajadores"
            if not _notif_exists('coverage_gap', title):
                db.session.add(Notification(
                    type='coverage_gap', title=title, severity='info',
                    link='/admin/shifts',
                ))
                created += 1

    if created:
        db.session.commit()
    return created


# ── API: unread count (polled from navbar) ─────────────────────────────────

@bp.route('/api/notifications/unread-count')
def unread_count():
    if not current_user.is_authenticated:
        return jsonify({'count': 0})
    cutoff = datetime.now() - timedelta(days=7)
    count = Notification.query.filter(
        Notification.read == False,  # noqa: E712
        Notification.created_at >= cutoff,
    ).count()
    return jsonify({'count': count})


# ── Admin page: list notifications ─────────────────────────────────────────

@bp.route('/admin/notifications')
@admin_required
def admin_notifications():
    page = request.args.get('page', 1, type=int)
    notif_type = request.args.get('type', '')
    severity = request.args.get('severity', '')
    status = request.args.get('status', '')  # read / unread / ''

    query = Notification.query

    if notif_type:
        query = query.filter(Notification.type == notif_type)
    if severity:
        query = query.filter(Notification.severity == severity)
    if status == 'read':
        query = query.filter(Notification.read == True)  # noqa: E712
    elif status == 'unread':
        query = query.filter(Notification.read == False)  # noqa: E712

    query = query.order_by(Notification.created_at.desc())
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    filters = {
        'type': notif_type,
        'severity': severity,
        'status': status,
    }
    return render_template('admin_notifications.html',
                           notifications=pagination.items,
                           pagination=pagination,
                           filters=filters)


# ── AJAX: mark single as read ──────────────────────────────────────────────

@bp.route('/admin/notifications/<int:id>/read', methods=['POST'])
@admin_required
def mark_read(id: int):
    notif = Notification.query.get_or_404(id)
    notif.read = True
    db.session.commit()
    return jsonify({'ok': True})


# ── AJAX: mark all as read ─────────────────────────────────────────────────

@bp.route('/admin/notifications/read-all', methods=['POST'])
@admin_required
def mark_all_read():
    Notification.query.filter(Notification.read == False).update({'read': True})  # noqa: E712
    db.session.commit()
    return jsonify({'ok': True})


# ── Manual trigger: generate ───────────────────────────────────────────────

@bp.route('/admin/notifications/generate', methods=['POST'])
@admin_required
def generate():
    count = _generate_notifications()
    return jsonify({'ok': True, 'created': count})
