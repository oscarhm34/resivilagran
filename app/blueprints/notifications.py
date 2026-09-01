"""Notification center: in-panel notifications for the admin dashboard."""
from __future__ import annotations

from flask import Blueprint, request, jsonify, render_template
from flask_login import current_user, login_required
from datetime import datetime, timedelta, date

from .. import app, db
from flask_jwt_extended import jwt_required, get_jwt_identity

from ..models import (Notification, VitalSignReading, VitalSignType,
                      CareRecord, CleaningRecord, Resident, Cleaner,
                      Incident, LegalDocument, DocumentSignature,
                      ShiftAssignment, ShiftCoverageRequirement, ShiftType,
                      AppSetting, PushSubscription)
from ..utils import admin_required, _safe_commit
import json as _json

bp = Blueprint('notifications', __name__)


# ── Helper: duplicate check ────────────────────────────────────────────────

def _notif_exists(notif_type: str, title: str, hours: float = 24,
                  link: str | None = None, worker_id: int | None = None) -> bool:
    """True si ya existe un aviso equivalente en las ultimas N horas.

    Con `link` deduplica por enlace y destinatario. Lo usan los avisos de
    sesion abierta, cuyo titulo lleva los minutos y cambia a cada pasada; antes
    se guardaba una clave en `message` para esto, pero eso hacia que la fila del
    panel se pintara como desplegable en vez de como enlace. Cada sesion genera
    dos copias, la del admin y la de la trabajadora, y cada una se deduplica por
    separado.
    """
    cutoff = datetime.now() - timedelta(hours=hours)
    query = Notification.query.filter(
        Notification.type == notif_type,
        Notification.created_at >= cutoff,
    )
    if link is not None:
        query = query.filter(Notification.link == link,
                             Notification.worker_id == worker_id)
    else:
        query = query.filter(
            db.or_(Notification.title == title, Notification.message == title))
    return db.session.query(query.exists()).scalar()


def _avisa_sesion_abierta(titulo: str, link: str, cleaner_id: int) -> int:
    """Crea las dos copias del aviso de sesion abierta.

    La del admin lleva el enlace al listado donde puede cerrarla o borrarla; la
    de la trabajadora es la que alimenta su PWA y el push. Se cuentan aparte
    porque el panel solo lista las del admin.
    """
    creadas = 0
    for destinatario in (None, cleaner_id):
        if _notif_exists('stale_session_worker', '', hours=0.25,
                         link=link, worker_id=destinatario):
            continue
        db.session.add(Notification(
            type='stale_session_worker', title=titulo, severity='warning',
            worker_id=destinatario, link=link,
        ))
        creadas += 1
    return creadas


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

    # 4. stale_session_worker — un aviso por sesion que pasa del umbral
    max_minutes = int(AppSetting.get('session_max_minutes', '120'))
    stale_threshold = now - timedelta(minutes=max_minutes)

    stale_worker_cleanings = CleaningRecord.query.filter(
        CleaningRecord.end_time.is_(None),
        CleaningRecord.start_time < stale_threshold,
    ).all()
    for rec in stale_worker_cleanings:
        room_desc = f'Hab. {rec.room.number}' if rec.room else 'Habitación'
        mins = int((now - rec.start_time).total_seconds() / 60)
        # El enlace lleva al listado en curso, con la fila resaltada, que es
        # donde estan los botones de cerrar y eliminar. Ademas hace de clave
        # de deduplicacion: el titulo cambia cada pasada porque lleva minutos.
        link = f'/registros-limpieza?estado=abiertas#rec-{rec.id}'
        titulo = f"Limpieza en {room_desc} lleva {mins} min abierta"
        created += _avisa_sesion_abierta(titulo, link, rec.cleaner_id)

    stale_worker_cares = CareRecord.query.filter(
        CareRecord.end_time.is_(None),
        CareRecord.start_time < stale_threshold,
    ).all()
    for rec in stale_worker_cares:
        resident_name = rec.resident.name if rec.resident else 'Residente'
        mins = int((now - rec.start_time).total_seconds() / 60)
        link = f'/registros-atencion?estado=abiertas#rec-{rec.id}'
        titulo = f"Atención con {resident_name} lleva {mins} min abierta"
        created += _avisa_sesion_abierta(titulo, link, rec.worker_id)

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
        ok, error = _safe_commit('Error al generar las notificaciones')
        if not ok:
            app.logger.error('No se pudieron generar las notificaciones: %s', error)
            return 0
        # Send push for worker-targeted notifications created in this batch
        _send_pending_pushes()

    # AI insights (throttled, async-safe)
    try:
        created += _generate_ai_insights()
    except Exception:
        pass

    # Auto shift handover (throttled, near shift boundaries)
    try:
        created += _generate_handover_notification()
    except Exception:
        pass

    return created


def _send_pending_pushes():
    """Send web push for recent unread worker notifications (last 2 minutes)."""
    try:
        cutoff = datetime.now() - timedelta(minutes=2)
        recent = Notification.query.filter(
            Notification.worker_id.isnot(None),
            Notification.read == False,  # noqa: E712
            Notification.created_at >= cutoff,
        ).all()
        for n in recent:
            send_push_for_notification(n)
    except Exception:
        pass


# ── Auto shift handover ──────────────────────────────────────────────────────

def _turno_siguiente(actual, shift_types):
    """El turno que empieza justo despues de que termine `actual`.

    Si ninguno empieza mas tarde, el relevo lo recoge el primero del dia
    siguiente (turno de noche que entrega al de manana).
    """
    def minutos(t):
        return t.hour * 60 + t.minute

    if not actual.end_time:
        return None
    fin = minutos(actual.end_time)
    candidatos = [t for t in shift_types
                  if t.id != actual.id and t.start_time is not None]
    if not candidatos:
        return None
    posteriores = [t for t in candidatos if minutos(t.start_time) >= fin]
    if posteriores:
        return min(posteriores, key=lambda t: minutos(t.start_time))
    return min(candidatos, key=lambda t: minutos(t.start_time))


def _generate_handover_notification() -> int:
    """Auto-generate handover report when a shift ends (within 30 min window). Throttled per shift."""
    now = datetime.now()
    current_time = now.time()

    shift_types = ShiftType.query.filter_by(active=True).all() if hasattr(ShiftType, 'active') else ShiftType.query.all()

    for st in shift_types:
        if not st.end_time:
            continue
        # Check if we are within 30 minutes after this shift's end_time
        shift_end = st.end_time
        # Compute minutes since shift end
        shift_end_minutes = shift_end.hour * 60 + shift_end.minute
        current_minutes = current_time.hour * 60 + current_time.minute
        diff = current_minutes - shift_end_minutes
        if diff < 0 or diff > 30:
            continue

        # Throttle: check if handover notification already generated for this shift today
        dedup_title = f'Traspaso automático — Turno {st.short_name} {now.strftime("%d/%m/%Y")}'
        if _notif_exists('shift_handover', dedup_title, hours=12):
            continue

        # Compute shift duration in hours
        start_minutes = st.start_time.hour * 60 + st.start_time.minute if st.start_time else 0
        hours = (shift_end_minutes - start_minutes) / 60
        if hours <= 0:
            hours += 24  # overnight shift

        # Generate handover using the same logic as the manual endpoint
        try:
            from .assessments import _call_claude, HANDOVER_SYSTEM
            from ..models import (CareRecord as CR, CleaningRecord as CLR,
                                  Incident as Inc, Activity as Act, Absence)

            cutoff = now - timedelta(hours=hours)
            lines = []

            # Shift workers
            today = now.date()
            assignments = ShiftAssignment.query.filter(
                ShiftAssignment.date == today,
                ShiftAssignment.shift_type_id == st.id,
            ).all()
            absent_ids = {a.cleaner_id for a in Absence.query.filter(
                Absence.start_date <= today, Absence.end_date >= today).all()}
            on_shift = [a for a in assignments if a.cleaner_id not in absent_ids]
            worker_names = []
            for a in on_shift:
                c = db.session.get(Cleaner, a.cleaner_id)
                if c:
                    worker_names.append(c.name)
            lines.append(f"TRASPASO AUTOMATICO — Turno {st.short_name} ({st.name}) — {now.strftime('%d/%m/%Y %H:%M')}")
            lines.append(f"Trabajadoras en el turno: {len(on_shift)} ({', '.join(worker_names[:10])})")

            # Care records during shift
            from sqlalchemy.orm import joinedload
            from ..models import VitalSignReading, VitalSignType
            care_records = CR.query.options(
                joinedload(CR.worker), joinedload(CR.resident),
                joinedload(CR.care_types),
                joinedload(CR.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
            ).filter(CR.start_time >= cutoff).order_by(CR.start_time.desc()).all()

            lines.append(f"\nATENCIONES: {len(care_records)}")
            notes_list = []
            for cr in care_records:
                w_name = cr.worker.name if cr.worker else '?'
                r_name = cr.resident.name if cr.resident else '?'
                types = ', '.join(ct.name for ct in cr.care_types) if cr.care_types else '?'
                vitals_parts = []
                for reading in cr.vital_sign_readings:
                    vst = reading.vital_sign_type
                    if vst:
                        vitals_parts.append(f"{vst.name}: {reading.value}{vst.unit}")
                vitals_str = f" | {', '.join(vitals_parts)}" if vitals_parts else ''
                lines.append(f"  {cr.start_time.strftime('%H:%M')} {r_name}: {types} ({w_name}){vitals_str}")
                if cr.notes:
                    notes_list.append(f"  {r_name}: {cr.notes}")

            # Cleanings
            cleanings = CLR.query.filter(
                CLR.start_time >= cutoff, CLR.end_time.isnot(None),
            ).count()
            lines.append(f"\nLIMPIEZAS: {cleanings}")

            # Incidents
            incidents = Inc.query.filter(Inc.created_at >= cutoff).all()
            if incidents:
                lines.append(f"\nINCIDENCIAS: {len(incidents)}")
                for inc in incidents:
                    lines.append(f"  {inc.title} (sev: {inc.severity})")

            if notes_list:
                lines.append("\nNOTAS:")
                for n in notes_list[:15]:
                    lines.append(n)

            context = '\n'.join(lines)
            html = _call_claude(HANDOVER_SYSTEM, f"Genera un informe de traspaso basado en:\n\n{context}")
            html = html.strip()
            if html.startswith('```'):
                html = html.split('\n', 1)[1] if '\n' in html else html[3:]
            if html.endswith('```'):
                html = html.rsplit('```', 1)[0]

            # Create notification for admin (no link — content shown inline)
            db.session.add(Notification(
                type='shift_handover',
                title=dedup_title,
                message=html,
                severity='info',
            ))

            # Una copia para cada trabajadora del turno SIGUIENTE. Antes se
            # cogian todas las asignaciones de todos los demas turnos del dia,
            # asi que el informe llegaba tambien a quien entraba mucho despues.
            siguiente = _turno_siguiente(st, shift_types)
            if siguiente is not None:
                entrantes = ShiftAssignment.query.filter(
                    ShiftAssignment.date == today,
                    ShiftAssignment.shift_type_id == siguiente.id,
                ).all()
                for ns in entrantes:
                    if ns.cleaner_id not in absent_ids:
                        db.session.add(Notification(
                            type='shift_handover',
                            title=f'Informe del turno anterior ({st.short_name})',
                            message=html,
                            severity='info',
                            worker_id=ns.cleaner_id,
                        ))

            ok, error = _safe_commit('Error al generar el relevo de turno')
            if not ok:
                app.logger.error('No se pudo generar el relevo de turno: %s', error)
                return 0
            return 1
        except Exception:
            return 0

    return 0


# ── AI-powered pattern detection ──────────────────────────────────────────────

def _generate_ai_insights() -> int:
    """Analyze residents with AI to detect concerning patterns. Throttled to 1x/6h."""
    # Throttle check
    last_run = AppSetting.get('ai_insights_last_run', '')
    if last_run:
        try:
            last_dt = datetime.fromisoformat(last_run)
            if (datetime.now() - last_dt).total_seconds() < 6 * 3600:
                return 0
        except (ValueError, TypeError):
            pass

    from .. import app
    api_key = app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return 0

    AppSetting.set('ai_insights_last_run', datetime.now().isoformat())
    ok, error = _safe_commit('Error al guardar la marca de los avisos automaticos')
    if not ok:
        app.logger.error('No se pudo guardar la marca de los avisos automaticos: %s', error)
        return 0

    from .assessments import _build_resident_context, _context_to_text

    residents = Resident.query.filter_by(active=True).all()
    if not residents:
        return 0

    # Build context for all residents with enough data
    contexts = []
    for r in residents:
        ctx = _build_resident_context(r.id, days=30)
        if not ctx:
            continue
        total_records = len(ctx.get('care_records', [])) + len(ctx.get('vital_signs', []))
        if total_records < 3:
            continue
        contexts.append((r.id, r.name, _context_to_text(ctx, 30)))

    if not contexts:
        return 0

    # Build prompt with all residents (limit to 15 to avoid token overflow)
    resident_texts = []
    for rid, name, text in contexts[:15]:
        resident_texts.append(f"--- RESIDENTE ID {rid} ---\n{text}")

    full_context = '\n\n'.join(resident_texts)

    system = """Eres un sistema de alerta clinica de la residencia de ancianos La Vila Gran.
Analiza los datos de los residentes y detecta SOLO tendencias preocupantes que requieran atencion.
Busca: perdida de independencia, aumento de caidas, deterioro cognitivo, perdida de peso,
constantes vitales con tendencia sostenida, residentes con pocas atenciones, observaciones
preocupantes del personal.
Si un residente esta estable, NO lo incluyas.
Responde SOLO con un JSON array. Si no hay alertas, responde [].
Formato: [{"resident_id": 123, "resident_name": "Nombre", "finding": "Descripcion breve del hallazgo", "recommendation": "Accion recomendada", "severity": "warning|info"}]"""

    prompt = f"Analiza los datos de los ultimos 30 dias de estos residentes:\n\n{full_context}"

    try:
        from .assessments import _call_claude
        response = _call_claude(system, prompt)

        # Parse JSON from response (handle markdown code blocks)
        import json as _json
        text = response.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1] if '\n' in text else text[3:]
            text = text.rsplit('```', 1)[0]
        insights = _json.loads(text)
    except Exception:
        return 0

    created = 0
    for insight in insights:
        rid = insight.get('resident_id')
        name = insight.get('resident_name', '')
        finding = insight.get('finding', '')
        recommendation = insight.get('recommendation', '')
        severity = insight.get('severity', 'info')
        if severity not in ('warning', 'info'):
            severity = 'info'

        title = f"{name}: {finding}"
        if len(title) > 195:
            title = title[:195] + '...'

        if not _notif_exists('ai_insight', title, hours=48):
            db.session.add(Notification(
                type='ai_insight', title=title,
                message=recommendation, severity=severity,
                resident_id=rid,
                link=f'/admin/resident/{rid}' if rid else None,
            ))
            created += 1

    if created:
        ok, error = _safe_commit('Error al guardar los avisos automaticos')
        if not ok:
            app.logger.error('No se pudieron guardar los avisos automaticos: %s', error)
            return 0
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
    destinatario = request.args.get('destinatario', '')  # '' = admin / 'todas'

    query = Notification.query

    # Un aviso con worker_id va dirigido a esa trabajadora y ella ya lo recibe
    # en su app. Listarlos aqui hacia que el informe de relevo saliera repetido,
    # una vez por el admin y otra por cada trabajadora.
    if destinatario != 'todas':
        query = query.filter(Notification.worker_id.is_(None))

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
        'destinatario': destinatario,
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
    ok, error = _safe_commit('Error al marcar la notificacion como leida')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True})


# ── AJAX: mark all as read ─────────────────────────────────────────────────

@bp.route('/admin/notifications/read-all', methods=['POST'])
@admin_required
def mark_all_read():
    Notification.query.filter(Notification.read == False).update({'read': True})  # noqa: E712
    ok, error = _safe_commit('Error al marcar las notificaciones como leidas')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True})


# ── Manual trigger: generate ───────────────────────────────────────────────

@bp.route('/admin/notifications/generate', methods=['POST'])
@admin_required
def generate():
    count = _generate_notifications()
    return jsonify({'ok': True, 'created': count})


# ── Worker notification endpoints ─────────────────────────────────────────

@bp.route('/api/worker/notifications')
@jwt_required()
def worker_notifications():
    """Return unread notifications for the authenticated worker."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'count': 0, 'notifications': []}), 200

    # La PWA sondea cada 5 minutos y por trabajadora. Regenerar aqui el sistema
    # entero de avisos (documentos, cobertura, relevo, incidencias) eran decenas de
    # consultas x N trabajadoras x 12 veces/hora solo para leer un contador. Se
    # limita con la misma marca en AppSetting que ya usan los insights de IA.
    ultima = AppSetting.get('worker_notifs_last_run', '')
    hay_que_generar = True
    if ultima:
        try:
            if (datetime.now() - datetime.fromisoformat(ultima)).total_seconds() < 600:
                hay_que_generar = False
        except (ValueError, TypeError):
            pass
    if hay_que_generar:
        AppSetting.set('worker_notifs_last_run', datetime.now().isoformat())
        try:
            _generate_notifications()
        except Exception as e:
            app.logger.error('Error al generar los avisos de la trabajadora: %s', e)

    cutoff = datetime.now() - timedelta(days=2)
    notifs = Notification.query.filter(
        Notification.worker_id == worker.id,
        Notification.read == False,  # noqa: E712
        Notification.created_at >= cutoff,
    ).order_by(Notification.created_at.desc()).limit(10).all()

    return jsonify({
        'count': len(notifs),
        'notifications': [{
            'id': n.id,
            'type': n.type,
            'title': n.title,
            'severity': n.severity,
            'created_at': n.created_at.isoformat(),
        } for n in notifs],
    }), 200


@bp.route('/api/worker/notifications/<int:nid>/read', methods=['POST'])
@jwt_required()
def worker_mark_read(nid: int):
    """Mark a worker notification as read."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autorizado'}), 403
    notif = Notification.query.get_or_404(nid)
    if notif.worker_id != worker.id:
        return jsonify({'error': 'No autorizado'}), 403
    notif.read = True
    ok, error = _safe_commit('Error al marcar la notificacion como leida')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True})


# ── Web Push API ──────────────────────────────────────────────────────────

@bp.route('/api/push/vapid-public-key')
@jwt_required()
def vapid_public_key():
    """Return the VAPID public key for client-side subscription."""
    from flask import current_app
    key = current_app.config.get('VAPID_PUBLIC_KEY')
    if not key:
        return jsonify({'error': 'Push no configurado'}), 503
    return jsonify({'public_key': key})


@bp.route('/api/push/subscribe', methods=['POST'])
@jwt_required()
def push_subscribe():
    """Save a push subscription for the authenticated worker."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autorizado'}), 403

    data = request.get_json(silent=True) or {}
    endpoint = (data.get('endpoint') or '').strip()
    keys = data.get('keys') or {}

    if not endpoint or not keys.get('p256dh') or not keys.get('auth'):
        return jsonify({'error': 'Datos de suscripcion incompletos'}), 400

    # Upsert: update if endpoint exists, create if not
    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing:
        existing.worker_id = worker.id
        existing.keys_json = _json.dumps(keys)
    else:
        db.session.add(PushSubscription(
            worker_id=worker.id,
            endpoint=endpoint,
            keys_json=_json.dumps(keys),
        ))
    ok, error = _safe_commit('Error al guardar la suscripcion de avisos')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True})


@bp.route('/api/push/unsubscribe', methods=['POST'])
@jwt_required()
def push_unsubscribe():
    """Remove a push subscription."""
    data = request.get_json(silent=True) or {}
    endpoint = (data.get('endpoint') or '').strip()
    if endpoint:
        PushSubscription.query.filter_by(endpoint=endpoint).delete()
        ok, error = _safe_commit('Error al eliminar la suscripcion de avisos')
        if not ok:
            return jsonify({'error': error}), 500
    return jsonify({'ok': True})


def send_push_to_worker(worker_id, title, body, url=None, tag=None):
    """Envia un aviso push a todas las suscripciones de una trabajadora.

    Cada motivo por el que no sale se registra en el log. Un aviso que no llega
    no deja rastro en ningun sitio: no hay pantalla que lo muestre y la persona
    solo sabe que no le suena el movil. Sin estas lineas, diagnosticarlo exige
    leer el codigo y adivinar.
    """
    from flask import current_app
    priv_key = current_app.config.get('VAPID_PRIVATE_KEY')
    email = current_app.config.get('VAPID_CLAIMS_EMAIL', 'mailto:admin@lavilagran.com')
    if not priv_key:
        app.logger.warning('Aviso push no enviado a %s: falta VAPID_PRIVATE_KEY', worker_id)
        return

    subs = PushSubscription.query.filter_by(worker_id=worker_id).all()
    if not subs:
        # Lo habitual: el navegador nunca llego a suscribirse (permiso denegado,
        # sin HTTPS, o un iPhone con la webapp sin anadir a la pantalla de inicio).
        app.logger.info('Aviso push no enviado a %s: no tiene ningun dispositivo suscrito', worker_id)
        return

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        app.logger.error('Aviso push no enviado: falta la libreria pywebpush')
        return

    payload = _json.dumps({
        'title': title,
        'body': body[:200] if body else '',
        'url': url or '/worker',
        # Agrupa los avisos de una misma conversacion: sin `tag`, el service
        # worker apila uno nuevo por mensaje y la barra de Android se llena.
        'tag': tag or 'lavilagran',
    })

    for sub in subs:
        try:
            keys = _json.loads(sub.keys_json)
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': keys,
                },
                data=payload,
                vapid_private_key=priv_key,
                vapid_claims={'sub': email},
            )
        except WebPushException as e:
            # 410 Gone o 404 = la suscripcion ya no vale, se descarta
            if '410' in str(e) or '404' in str(e):
                app.logger.info('Suscripcion caducada de %s, se elimina', worker_id)
                db.session.delete(sub)
                ok, err = _safe_commit('Error al limpiar la suscripcion caducada')
                if not ok:
                    app.logger.error('No se pudo limpiar la suscripcion caducada: %s', err)
            else:
                app.logger.error('El servicio de avisos rechazo el envio a %s: %s',
                                 worker_id, e)
        except Exception as e:
            app.logger.error('Error al enviar el aviso push a %s: %s', worker_id, e)


def send_push_for_notification(notification):
    """Send push for a newly created Notification if it has a worker_id."""
    if not notification.worker_id:
        return
    # `link` apunta al panel de administracion, donde la trabajadora no entra.
    # Su push abre la PWA, que es donde puede cerrar la sesion.
    send_push_to_worker(
        worker_id=notification.worker_id,
        title=notification.title,
        body=notification.title,
        url='/worker',
    )
