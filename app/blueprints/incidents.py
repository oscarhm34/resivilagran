"""Incidents module — admin CRUD + worker reporting API."""
from __future__ import annotations

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, abort
from flask_login import current_user
from flask_jwt_extended import jwt_required
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta

from .. import app, db, limiter
from ..models import Cleaner, Resident, IncidentType, Incident, AppSetting, FallRecord, Notification
from ..utils import admin_required, _verify_worker_id, _safe_commit, log_audit

bp = Blueprint('incidents', __name__)


# ── Admin — Incidents ───────────────────────────────────────────────────────

@bp.route('/admin/incidents')
@admin_required
def admin_incidents():
    query = Incident.query.options(
        joinedload(Incident.incident_type),
        joinedload(Incident.reporter),
        joinedload(Incident.resident),
    )

    # Filters
    incident_type_id = request.args.get('incident_type_id', type=int)
    severity = request.args.get('severity', '').strip()
    status = request.args.get('status', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()

    if incident_type_id:
        query = query.filter(Incident.incident_type_id == incident_type_id)
    if severity:
        query = query.filter(Incident.severity == severity)
    if status:
        query = query.filter(Incident.status == status)
    if start_date:
        try:
            query = query.filter(Incident.occurred_at >= datetime.strptime(start_date, '%Y-%m-%d'))
        except ValueError:
            pass
    if end_date:
        try:
            query = query.filter(Incident.occurred_at <= datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1))
        except ValueError:
            pass

    query = query.order_by(Incident.occurred_at.desc())
    page = request.args.get('page', 1, type=int)
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    filters = {
        'incident_type_id': incident_type_id or '',
        'severity': severity,
        'status': status,
        'start_date': start_date,
        'end_date': end_date,
    }

    return render_template(
        'admin_incidents.html',
        incidents=pagination.items,
        pagination=pagination,
        filters=filters,
        incident_types=IncidentType.query.filter_by(active=True).order_by(IncidentType.name).all(),
        workers=Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all(),
        residents=Resident.query.order_by(Resident.name).all(),
    )


@bp.route('/admin/incidents/create', methods=['POST'])
@admin_required
def create_incident():
    title = request.form.get('title', '').strip()
    if not title:
        flash('El título es obligatorio.', 'error')
        return redirect(url_for('incidents.admin_incidents'))

    occurred_at_str = request.form.get('occurred_at', '').strip()
    try:
        occurred_at = datetime.strptime(occurred_at_str, '%Y-%m-%dT%H:%M') if occurred_at_str else datetime.now()
    except ValueError:
        occurred_at = datetime.now()

    incident_type_id = request.form.get('incident_type_id', type=int)
    resident_id = request.form.get('resident_id', type=int)

    is_fall = request.form.get('is_fall') == 'on'

    incident = Incident(
        title=title,
        description=request.form.get('description', '').strip() or None,
        incident_type_id=incident_type_id or None,
        reported_by=current_user.id,
        resident_id=resident_id or None,
        location=request.form.get('location', '').strip() or None,
        severity=request.form.get('severity', 'medium'),
        occurred_at=occurred_at,
        is_fall=is_fall,
    )
    db.session.add(incident)
    db.session.flush()

    # Save structured fall data
    if is_fall:
        fall = FallRecord(
            incident_id=incident.id,
            fall_location=request.form.get('fall_location', '').strip() or None,
            activity_at_time=request.form.get('activity_at_time', '').strip() or None,
            footwear=request.form.get('footwear', '').strip() or None,
            witnesses=request.form.get('witnesses', '').strip() or None,
            injuries=request.form.get('injuries', '').strip() or None,
            injury_severity=request.form.get('injury_severity', 'none'),
            measures_taken=request.form.get('measures_taken', '').strip() or None,
            contributing_factors=request.form.get('contributing_factors', '').strip() or None,
        )
        db.session.add(fall)

    log_audit('create', 'incident', incident.id,
              {'severidad': incident.severity, 'es_caida': is_fall,
               'resident_id': incident.resident_id})
    ok, error = _safe_commit('Error al crear la incidencia')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('incidents.admin_incidents'))

    # Auto-review PAI if fall incident with resident
    if is_fall and incident.resident_id:
        try:
            from .assessments import auto_review_care_plan
            auto_review_care_plan(incident.resident_id,
                f'Caída registrada: {incident.title}. Severidad: {incident.severity}.')
        except Exception:
            pass

    flash('Incidencia creada correctamente.', 'success')
    return redirect(url_for('incidents.admin_incidents'))


@bp.route('/admin/incidents/<int:id>/edit', methods=['POST'])
@admin_required
def edit_incident(id: int):
    incident = db.session.get(Incident, id)
    if not incident:
        abort(404)

    title = request.form.get('title', '').strip()
    if not title:
        flash('El título es obligatorio.', 'error')
        return redirect(url_for('incidents.admin_incidents'))

    occurred_at_str = request.form.get('occurred_at', '').strip()
    try:
        incident.occurred_at = datetime.strptime(occurred_at_str, '%Y-%m-%dT%H:%M') if occurred_at_str else incident.occurred_at
    except ValueError:
        pass

    incident_type_id = request.form.get('incident_type_id', type=int)
    resident_id = request.form.get('resident_id', type=int)

    incident.title = title
    incident.description = request.form.get('description', '').strip() or None
    incident.incident_type_id = incident_type_id or None
    incident.resident_id = resident_id or None
    incident.location = request.form.get('location', '').strip() or None
    incident.severity = request.form.get('severity', 'medium')

    log_audit('update', 'incident', incident.id,
              {'severidad': incident.severity, 'resident_id': incident.resident_id})
    ok, error = _safe_commit('Error al actualizar la incidencia')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('incidents.admin_incidents'))
    flash('Incidencia actualizada.', 'success')
    return redirect(url_for('incidents.admin_incidents'))


@bp.route('/admin/incidents/<int:id>/update-status', methods=['POST'])
@admin_required
def update_incident_status(id: int):
    incident = db.session.get(Incident, id)
    if not incident:
        abort(404)

    new_status = request.form.get('status', '').strip()
    if new_status not in ('open', 'in_progress', 'resolved', 'closed'):
        flash('Estado no válido.', 'error')
        return redirect(url_for('incidents.admin_incidents'))

    incident.status = new_status

    if new_status == 'resolved':
        incident.resolved_at = datetime.now()
        incident.resolved_by = current_user.id
        incident.resolution_notes = request.form.get('resolution_notes', '').strip() or None

    log_audit('update', 'incident', incident.id, {'estado': new_status})
    ok, error = _safe_commit('Error al actualizar el estado de la incidencia')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('incidents.admin_incidents'))
    flash('Estado actualizado.', 'success')
    return redirect(url_for('incidents.admin_incidents'))


@bp.route('/admin/incidents/<int:id>/delete', methods=['POST'])
@admin_required
def delete_incident(id: int):
    incident = db.session.get(Incident, id)
    if not incident:
        abort(404)
    db.session.delete(incident)
    log_audit('delete', 'incident', id,
              {'severidad': incident.severity, 'resident_id': incident.resident_id})
    ok, error = _safe_commit('Error al eliminar la incidencia')
    if not ok:
        flash(error, 'danger')
    else:
        flash('Incidencia eliminada.', 'success')
    return redirect(url_for('incidents.admin_incidents'))


# ── Admin — Incident Types ─────────────────────────────────────────────────

@bp.route('/admin/incident-types')
@admin_required
def admin_incident_types():
    types = IncidentType.query.order_by(IncidentType.sort_order, IncidentType.name).all()
    return render_template('admin_incident_types.html', incident_types=types)


@bp.route('/admin/incident-types/add_edit', methods=['POST'])
@admin_required
def add_edit_incident_type():
    type_id = request.form.get('type_id')
    name = request.form.get('name', '').strip()

    if not name:
        flash('El nombre es obligatorio.', 'error')
        return redirect(url_for('incidents.admin_incident_types'))

    if type_id:
        it = db.session.get(IncidentType, int(type_id))
        if it:
            it.name = name
            it.icon = request.form.get('icon', '').strip() or None
            it.color = request.form.get('color', '').strip() or None
            it.severity = request.form.get('severity', 'medium')
            it.sort_order = int(request.form.get('sort_order', '0') or 0)
            log_audit('update', 'incident_type', it.id, {'nombre': name})
            ok, error = _safe_commit('Error al actualizar el tipo de incidencia')
            if not ok:
                flash(error, 'danger')
                return redirect(url_for('incidents.admin_incident_types'))
            flash('Tipo actualizado correctamente.', 'success')
        else:
            flash('Tipo no encontrado.', 'error')
    else:
        it = IncidentType(
            name=name,
            icon=request.form.get('icon', '').strip() or None,
            color=request.form.get('color', '').strip() or None,
            severity=request.form.get('severity', 'medium'),
            sort_order=int(request.form.get('sort_order', '0') or 0),
        )
        db.session.add(it)
        log_audit('create', 'incident_type', None, {'nombre': name})
        ok, error = _safe_commit('Error al crear el tipo de incidencia')
        if not ok:
            flash(error, 'danger')
            return redirect(url_for('incidents.admin_incident_types'))
        flash('Tipo añadido correctamente.', 'success')

    return redirect(url_for('incidents.admin_incident_types'))


@bp.route('/admin/incident-types/<int:id>/delete', methods=['POST'])
@admin_required
def delete_incident_type(id: int):
    it = db.session.get(IncidentType, id)
    if not it:
        abort(404)
    if it.incidents:
        flash('No se puede eliminar un tipo que tiene incidencias asociadas.', 'error')
    else:
        db.session.delete(it)
        log_audit('delete', 'incident_type', id, {'nombre': it.name})
        ok, error = _safe_commit('Error al eliminar el tipo de incidencia')
        if not ok:
            flash(error, 'danger')
        else:
            flash('Tipo eliminado.', 'success')
    return redirect(url_for('incidents.admin_incident_types'))


@bp.route('/admin/incident-types/toggle-active', methods=['POST'])
@admin_required
def toggle_incident_type_active():
    data = request.json or {}
    it = db.session.get(IncidentType, int(data.get('id', 0)))
    if not it:
        return jsonify({'error': 'No encontrado'}), 404
    it.active = data.get('active', True)
    log_audit('update', 'incident_type', it.id, {'activo': it.active})
    ok, error = _safe_commit('Error al cambiar el estado del tipo de incidencia')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True}), 200


# ── Worker API ──────────────────────────────────────────────────────────────

@bp.route('/api/worker/incidents')
@jwt_required()
def worker_incidents():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    if not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403

    incidents = Incident.query.filter_by(reported_by=worker_id)\
        .options(joinedload(Incident.incident_type), joinedload(Incident.resident))\
        .order_by(Incident.occurred_at.desc()).all()

    return jsonify([{
        'id': i.id,
        'title': i.title,
        'description': i.description or '',
        'severity': i.severity,
        'status': i.status,
        'occurred_at': i.occurred_at.isoformat() if i.occurred_at else None,
        'incident_type': i.incident_type.name if i.incident_type else None,
        'resident': i.resident.name if i.resident else None,
        'location': i.location or '',
    } for i in incidents])


@bp.route('/api/worker/incidents/report', methods=['POST'])
@jwt_required()
def worker_report_incident():
    if AppSetting.get('allow_worker_incidents', 'false') != 'true':
        return jsonify({'error': 'El reporte de incidencias no está habilitado'}), 403

    data = request.json or {}
    worker_id = data.get('worker_id')
    if not worker_id or not _verify_worker_id(int(worker_id)):
        return jsonify({'error': 'No autorizado'}), 403

    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'El título es obligatorio'}), 400

    occurred_at_str = data.get('occurred_at', '').strip()
    try:
        occurred_at = datetime.fromisoformat(occurred_at_str) if occurred_at_str else datetime.now()
    except ValueError:
        occurred_at = datetime.now()

    is_fall = data.get('is_fall', False)

    incident = Incident(
        title=title,
        description=(data.get('description') or '').strip() or None,
        incident_type_id=data.get('incident_type_id') or None,
        reported_by=int(worker_id),
        resident_id=data.get('resident_id') or None,
        location=(data.get('location') or '').strip() or None,
        severity=data.get('severity', 'medium'),
        occurred_at=occurred_at,
        is_fall=is_fall,
    )
    db.session.add(incident)
    db.session.flush()

    if is_fall:
        fall_data = data.get('fall_data', {})
        fall = FallRecord(
            incident_id=incident.id,
            fall_location=(fall_data.get('fall_location') or '').strip() or None,
            activity_at_time=(fall_data.get('activity_at_time') or '').strip() or None,
            footwear=(fall_data.get('footwear') or '').strip() or None,
            witnesses=(fall_data.get('witnesses') or '').strip() or None,
            injuries=(fall_data.get('injuries') or '').strip() or None,
            injury_severity=fall_data.get('injury_severity', 'none'),
            measures_taken=(fall_data.get('measures_taken') or '').strip() or None,
            contributing_factors=(fall_data.get('contributing_factors') or '').strip() or None,
        )
        db.session.add(fall)

    log_audit('create', 'incident', incident.id,
              {'severidad': incident.severity, 'es_caida': is_fall,
               'resident_id': incident.resident_id, 'origen': 'trabajadora',
               'cleaner_id': int(worker_id)})
    ok, error = _safe_commit('Error al registrar la incidencia')
    if not ok:
        return jsonify({'error': error}), 500

    # Auto-review PAI if fall incident with resident
    if is_fall and incident.resident_id:
        try:
            from .assessments import auto_review_care_plan
            auto_review_care_plan(incident.resident_id,
                f'Caída registrada: {incident.title or "sin detalles"}. '
                f'Severidad: {incident.severity}.')
        except Exception:
            pass

    return jsonify({'ok': True, 'id': incident.id}), 201


# ── AI classification for incidents ──────────────────────────────────────────

@bp.route('/api/incidents/ai-classify', methods=['POST'])
@limiter.limit("5/minute")
@jwt_required()
def ai_classify_incident():
    """Use AI to pre-fill incident form fields from free text description."""
    data = request.json or {}
    description = (data.get('description') or data.get('title') or '').strip()

    if not description or len(description) < 5:
        return jsonify({'error': 'Texto requerido'}), 400

    api_key = app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'IA no disponible'}), 503

    # Get available incident types
    types = IncidentType.query.filter_by(active=True).order_by(IncidentType.name).all()
    types_list = ', '.join(f'{t.id}={t.name}' for t in types)

    system = f"""Eres un asistente de documentación de incidencias en la residencia de ancianos La Vila Gran.
Dada la descripción libre de una incidencia, extrae y sugiere TODOS los campos estructurados posibles.

Tipos de incidencia disponibles: {types_list}

Responde SOLO con JSON válido con estos campos:
{{
  "title": "<titulo corto y descriptivo de la incidencia, max 60 chars>",
  "incident_type_id": <id del tipo mas apropiado>,
  "severity": "<low|medium|high|critical>",
  "location": "<lugar donde ocurrió si se menciona, o null>",
  "is_fall": <true si es una caída, false si no>,
  "fall_data": {{
    "fall_location": "<lugar exacto de la caída o null>",
    "activity_at_time": "<que hacía el residente cuando cayó o null>",
    "footwear": "<calzado que llevaba o null>",
    "witnesses": "<testigos o null>",
    "injuries": "<lesiones observadas o null>",
    "injury_severity": "<none|minor|moderate|severe>",
    "measures_taken": "<medidas tomadas o null>",
    "contributing_factors": "<factores contribuyentes o null>"
  }},
  "description": "<descripción profesional expandida de la incidencia, 2-3 frases>"
}}
Si no es una caída, fall_data debe ser null. No inventes información que no esté en el texto."""

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=500,
            system=system,
            messages=[{'role': 'user', 'content': f'Descripcion del trabajador: "{description}"'}],
        )
        result = ''.join(b.text for b in response.content if hasattr(b, 'text'))
        result = result.strip()
        if result.startswith('```'):
            result = result.split('\n', 1)[1] if '\n' in result else result[3:]
        if result.endswith('```'):
            result = result.rsplit('```', 1)[0]

        import json as _json
        suggestion = _json.loads(result.strip())
        return jsonify(suggestion)
    except Exception:
        return jsonify({'error': 'No se ha podido clasificar'}), 500
