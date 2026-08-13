"""Admin routes: auth, dashboard, workers, rooms, floors, zones, cleaning records, analytics, help."""
from __future__ import annotations

from flask import (Blueprint, request, jsonify, render_template, redirect,
                   url_for, flash, send_file, abort, current_app)
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta
import pandas as pd
from io import BytesIO
import json

from .. import db, limiter
from ..models import (Cleaner, Room, CleaningRecord, Floor, RoomType, Resident,
                      CareType, CareRecord, ResidentGroup,
                      VitalSignType, VitalSignReading,
                      ShiftAssignment, ChecklistItem,
                      CleaningTargetTime, AuditLog)
from ..utils import (admin_required, _format_duration,
                     _compute_cleaning_stats, _calculate_room_urgency)

bp = Blueprint('admin_bp', __name__)


# ── WEB AUTH ────────────────────────────────────────────────────────────────

@bp.route('/admin/login', methods=['GET', 'POST'])
@limiter.limit("10/minute", methods=["POST"])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_bp.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = Cleaner.query.filter_by(username=username).first()

        if user and user.check_password(password) and user.is_admin:
            login_user(user)
            next_page = request.args.get('next', '')
            # Validate: must be relative path, no protocol, no double-slash
            if (not next_page or not next_page.startswith('/')
                    or next_page.startswith('//') or ':' in next_page):
                next_page = url_for('admin_bp.index')
            return redirect(next_page)

        flash('Credenciales incorrectas o sin permisos de administrador.', 'danger')

    return render_template('login.html')


@bp.route('/admin/logout', methods=['POST'])
@login_required
def admin_logout():
    logout_user()
    flash('Sesión cerrada correctamente.', 'success')
    return redirect(url_for('admin_bp.admin_login'))


# ── HOME ────────────────────────────────────────────────────────────────────

@bp.route('/')
@admin_required
def index():
    # Auto-close stale sessions older than 24 hours
    stale_cutoff = datetime.now() - timedelta(hours=24)
    stale_cleanings = CleaningRecord.query.filter(
        CleaningRecord.end_time.is_(None),
        CleaningRecord.start_time < stale_cutoff,
    ).all()
    stale_cares = CareRecord.query.filter(
        CareRecord.end_time.is_(None),
        CareRecord.start_time < stale_cutoff,
    ).all()
    stale_count = len(stale_cleanings) + len(stale_cares)
    for s in stale_cleanings:
        s.end_time = stale_cutoff  # Close at the 24h cutoff, not +1h from start
    for s in stale_cares:
        s.end_time = stale_cutoff
    if stale_count:
        db.session.commit()

    try:
        from .notifications import _generate_notifications
        _generate_notifications()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('Error generating notifications: %s', e)

    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    hoy_inicio = datetime.combine(today, datetime.min.time())
    hoy_fin = datetime.combine(tomorrow, datetime.min.time())

    limpiezas_hoy = CleaningRecord.query.filter(
        CleaningRecord.start_time >= hoy_inicio,
        CleaningRecord.start_time < hoy_fin,
    ).count()

    en_curso = CleaningRecord.query.filter(CleaningRecord.end_time.is_(None)).count()

    limpiadas_hoy_ids = [
        r[0] for r in db.session.query(CleaningRecord.room_id)
        .filter(
            CleaningRecord.end_time.isnot(None),
            CleaningRecord.start_time >= hoy_inicio,
            CleaningRecord.start_time < hoy_fin,
        )
        .distinct()
        .all()
    ]
    total_rooms = Room.query.count()
    if limpiadas_hoy_ids:
        habitaciones_sin_limpiar = total_rooms - len(limpiadas_hoy_ids)
    else:
        habitaciones_sin_limpiar = total_rooms

    total_residents_active = Resident.query.filter_by(active=True).count()
    total_workers_active = Cleaner.query.filter_by(active=True).count()
    workers_with_sessions_today = db.session.query(
        db.func.count(db.func.distinct(CareRecord.worker_id))
    ).filter(
        CareRecord.start_time >= hoy_inicio,
        CareRecord.start_time < hoy_fin,
    ).scalar() or 0

    atenciones_hoy = CareRecord.query.filter(
        CareRecord.start_time >= hoy_inicio,
        CareRecord.start_time < hoy_fin,
    ).count()

    atenciones_en_curso = CareRecord.query.filter(CareRecord.end_time.is_(None)).count()

    alertas_vitales = []
    vitals_hoy = VitalSignReading.query.options(
        joinedload(VitalSignReading.vital_sign_type),
        joinedload(VitalSignReading.care_record).joinedload(CareRecord.resident),
    ).filter(VitalSignReading.recorded_at >= hoy_inicio).all()
    for r in vitals_hoy:
        vst = r.vital_sign_type
        is_low = vst.min_value is not None and r.value < vst.min_value
        is_high = vst.max_value is not None and r.value > vst.max_value
        if is_low or is_high:
            alertas_vitales.append({
                'resident': r.care_record.resident.name if r.care_record.resident else '?',
                'type': vst.name,
                'value': r.value,
                'unit': vst.unit,
                'alert': 'alta' if is_high else 'baja',
                'time': r.recorded_at.strftime('%H:%M'),
            })

    # Open incidents
    from ..models import Incident, ShiftAssignment, ShiftType, LegalDocument, DocumentSignature, Notification
    open_incidents = Incident.query.filter(Incident.status.in_(['open', 'in_progress'])).count()
    critical_incidents = Incident.query.filter(
        Incident.status.in_(['open', 'in_progress']),
        Incident.severity.in_(['critical', 'high']),
    ).count()

    # Workers on shift today
    from ..models import Absence
    shifts_today = ShiftAssignment.query.options(
        joinedload(ShiftAssignment.cleaner), joinedload(ShiftAssignment.shift_type),
    ).filter(
        ShiftAssignment.date == today,
        ShiftAssignment.shift_type_id.isnot(None),
    ).all()
    absent_ids = {a.cleaner_id for a in Absence.query.filter(
        Absence.start_date <= today, Absence.end_date >= today,
    ).all()}
    workers_on_shift = [s for s in shifts_today if s.cleaner_id not in absent_ids]
    shift_summary = {}
    for s in workers_on_shift:
        name = s.shift_type.short_name if s.shift_type else '?'
        shift_summary[name] = shift_summary.get(name, 0) + 1

    # Pending documents
    total_active_docs = LegalDocument.query.filter_by(active=True).count()
    total_signatures_needed = total_active_docs * total_workers_active if total_active_docs else 0
    total_signatures_done = DocumentSignature.query.count()
    pending_signatures = max(0, total_signatures_needed - total_signatures_done)

    # Unread notifications
    from datetime import date as date_type
    week_ago = datetime.now() - timedelta(days=7)
    unread_notifs = Notification.query.filter(
        Notification.read == False,
        Notification.created_at >= week_ago,
    ).count()

    return render_template(
        'index.html',
        limpiezas_hoy=limpiezas_hoy,
        en_curso=en_curso,
        habitaciones_sin_limpiar=habitaciones_sin_limpiar,
        atenciones_hoy=atenciones_hoy,
        atenciones_en_curso=atenciones_en_curso,
        stale_closed=stale_count,
        alertas_vitales=alertas_vitales,
        total_residents_active=total_residents_active,
        total_workers_active=total_workers_active,
        workers_with_sessions_today=workers_with_sessions_today,
        open_incidents=open_incidents,
        critical_incidents=critical_incidents,
        workers_on_shift=len(workers_on_shift),
        shift_summary=shift_summary,
        pending_signatures=pending_signatures,
        unread_notifs=unread_notifs,
    )


# ── WEB ADMIN – EMPLEADOS ──────────────────────────────────────────────────

@bp.route('/manage_workers')
@admin_required
def manage_workers():
    estado = request.args.get('estado', 'altas')
    query = Cleaner.query
    if estado == 'altas':
        query = query.filter_by(active=True)
    elif estado == 'bajas':
        query = query.filter_by(active=False)
    cleaners = query.all()
    groups = ResidentGroup.query.order_by(ResidentGroup.name).all()
    return render_template('manage_workers.html', cleaners=cleaners, groups=groups, estado_filtro=estado)


@bp.route('/cleaners/add_edit', methods=['POST'])
@admin_required
def add_edit_cleaner():
    cleaner_id = request.form.get('cleaner_id')
    username = request.form.get('username', '').strip()
    name = request.form.get('name', '').strip()
    password = request.form.get('password', '')
    is_admin = bool(request.form.get('is_admin'))
    active = bool(request.form.get('active'))
    role = request.form.get('role', 'atenciones').strip()
    if role not in ('limpieza', 'atenciones', 'mixto', 'gestion'):
        role = 'atenciones'

    group_ids = request.form.getlist('group_ids')
    selected_groups = ResidentGroup.query.filter(ResidentGroup.id.in_(group_ids)).all() if group_ids else []

    if cleaner_id:
        cleaner = db.session.get(Cleaner, int(cleaner_id))
        if cleaner:
            cleaner.username = username
            cleaner.name = name
            cleaner.is_admin = is_admin
            cleaner.active = active
            cleaner.role = role
            cleaner.groups = selected_groups
            if password:
                cleaner.set_password(password)
            db.session.commit()
            flash('Trabajador actualizado correctamente.', 'success')
        else:
            flash('Trabajador no encontrado.', 'error')
    else:
        new_cleaner = Cleaner(username=username, name=name, is_admin=is_admin, active=active, role=role)
        new_cleaner.set_password(password)
        new_cleaner.groups = selected_groups
        db.session.add(new_cleaner)
        db.session.commit()
        flash('Trabajador añadido correctamente.', 'success')

    return redirect(url_for('admin_bp.manage_workers'))


@bp.route('/cleaners/delete/<int:id>', methods=['POST'])
@admin_required
def delete_cleaner(id: int):
    try:
        cleaner = db.session.get(Cleaner, id)
        if cleaner is None:
            abort(404)
        db.session.delete(cleaner)
        db.session.commit()
        flash('Trabajador eliminado con éxito.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque tiene registros de limpieza asociados.', 'error')
    return redirect(url_for('admin_bp.manage_workers'))


@bp.route('/cleaners/update-groups', methods=['POST'])
@admin_required
def update_cleaner_groups():
    data = request.json or {}
    cleaner_id = data.get('cleaner_id')
    group_ids = data.get('group_ids', [])
    if not cleaner_id:
        return jsonify({'error': 'cleaner_id requerido'}), 400
    cleaner = db.session.get(Cleaner, int(cleaner_id))
    if not cleaner:
        return jsonify({'error': 'Empleado no encontrado'}), 404
    cleaner.groups = ResidentGroup.query.filter(ResidentGroup.id.in_(group_ids)).all() if group_ids else []
    db.session.commit()
    return jsonify({'ok': True}), 200


@bp.route('/cleaners/update-active', methods=['POST'])
@admin_required
def update_cleaner_active():
    data = request.json or {}
    cleaner_id = data.get('cleaner_id')
    active = data.get('active')
    if not cleaner_id or active is None:
        return jsonify({'error': 'cleaner_id y active requeridos'}), 400
    cleaner = db.session.get(Cleaner, int(cleaner_id))
    if not cleaner:
        return jsonify({'error': 'Empleado no encontrado'}), 404
    cleaner.active = bool(active)
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── WEB ADMIN – ZONAS DE LIMPIEZA ──────────────────────────────────────────

@bp.route('/zonas-limpieza')
@admin_required
def manage_cleaning_zones():
    rooms = Room.query.all()
    floors = Floor.query.all()
    room_types = RoomType.query.all()
    return render_template(
        'manage_cleaning_zones.html',
        rooms=rooms, floors=floors, room_types=room_types, form_data={}
    )


@bp.route('/rooms/add_edit', methods=['POST'])
@admin_required
def add_edit_room():
    room_id = request.form.get('room_id')
    number = request.form.get('number', '').strip()
    room_type_id = request.form.get('room_type_id')
    floor_id = request.form.get('floor_id')
    description = request.form.get('description', '').strip()

    if not number:
        flash('El número de la habitación es requerido.', 'error')
    elif not room_type_id:
        flash('El tipo de espacio es requerido.', 'error')
    elif not floor_id:
        flash('La planta es requerida.', 'error')
    elif room_id:
        room = db.session.get(Room, int(room_id))
        if room:
            room.number = number
            room.room_type_id = room_type_id
            room.floor_id = floor_id
            room.description = description
            db.session.commit()
            flash('Espacio actualizado correctamente.', 'success')
        else:
            flash('Espacio no encontrado.', 'error')
    else:
        db.session.add(Room(number=number, room_type_id=room_type_id, floor_id=floor_id, description=description))
        db.session.commit()
        flash('Espacio añadido correctamente.', 'success')

    return redirect(url_for('admin_bp.manage_cleaning_zones'))


@bp.route('/rooms/delete/<int:id>', methods=['POST'])
@admin_required
def delete_room(id: int):
    try:
        room = db.session.get(Room, id)
        if room is None:
            abort(404)
        db.session.delete(room)
        db.session.commit()
        flash('Espacio eliminado con éxito.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque está en uso.', 'error')
    return redirect(url_for('admin_bp.manage_cleaning_zones'))


# ── WEB ADMIN – TIPOS DE ESPACIO ───────────────────────────────────────────

@bp.route('/manage_room_types')
@admin_required
def manage_room_types():
    room_types = RoomType.query.all()
    return render_template('manage_room_types.html', room_types=room_types)


@bp.route('/room_types/add_edit', methods=['POST'])
@admin_required
def add_edit_room_type():
    room_type_id = request.form.get('room_type_id')
    name = request.form.get('name', '').strip()

    if room_type_id:
        room_type = db.session.get(RoomType, int(room_type_id))
        if room_type:
            room_type.name = name
            db.session.commit()
            flash('Tipo de espacio actualizado correctamente.', 'success')
        else:
            flash('Tipo de espacio no encontrado.', 'error')
    else:
        db.session.add(RoomType(name=name))
        db.session.commit()
        flash('Tipo de espacio añadido correctamente.', 'success')
    return redirect(url_for('admin_bp.manage_room_types'))


@bp.route('/room_types/delete/<int:id>', methods=['POST'])
@admin_required
def delete_room_type(id: int):
    try:
        room_type = db.session.get(RoomType, id)
        if room_type is None:
            abort(404)
        db.session.delete(room_type)
        db.session.commit()
        flash('Tipo de espacio eliminado correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque está en uso.', 'error')
    return redirect(url_for('admin_bp.manage_room_types'))


# ── WEB ADMIN – PLANTAS ────────────────────────────────────────────────────

@bp.route('/manage_floors')
@admin_required
def manage_floors():
    floors = Floor.query.all()
    return render_template('manage_floors.html', floors=floors)


@bp.route('/floors/add_edit', methods=['POST'])
@admin_required
def add_edit_floor():
    floor_id = request.form.get('floor_id')
    name = request.form.get('name', '').strip()

    if floor_id:
        floor = db.session.get(Floor, int(floor_id))
        if floor:
            floor.name = name
            db.session.commit()
            flash('Planta actualizada correctamente.', 'success')
        else:
            flash('Planta no encontrada.', 'error')
    else:
        db.session.add(Floor(name=name))
        db.session.commit()
        flash('Planta añadida correctamente.', 'success')
    return redirect(url_for('admin_bp.manage_floors'))


@bp.route('/floors/delete/<int:id>', methods=['POST'])
@admin_required
def delete_floor(id: int):
    try:
        floor = db.session.get(Floor, id)
        if floor is None:
            abort(404)
        db.session.delete(floor)
        db.session.commit()
        flash('Planta eliminada correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque está en uso.', 'error')
    return redirect(url_for('admin_bp.manage_floors'))


# ── WEB ADMIN – REGISTROS DE LIMPIEZA ──────────────────────────────────────

@bp.route('/admin/close-session/<mode>/<int:record_id>', methods=['POST'])
@admin_required
def admin_close_session(mode: str, record_id: int):
    now = datetime.now()
    if mode == 'cleaning':
        rec = db.session.get(CleaningRecord, record_id)
        if rec and not rec.end_time:
            rec.end_time = now
            db.session.commit()
            flash(f'Sesión de limpieza cerrada (Hab. {rec.room.number if rec.room else record_id}).', 'success')
        return redirect(request.referrer or url_for('admin_bp.registros_limpieza'))
    elif mode == 'care':
        rec = db.session.get(CareRecord, record_id)
        if rec and not rec.end_time:
            rec.end_time = now
            db.session.commit()
            flash(f'Sesión de atención cerrada ({rec.resident.name if rec.resident else record_id}).', 'success')
        return redirect(request.referrer or url_for('residents.registros_atencion'))
    abort(400)


@bp.route('/registros-limpieza')
@admin_required
def registros_limpieza():
    room_id = request.args.get('room_id', '')
    cleaner_id = request.args.get('cleaner_id', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    query = CleaningRecord.query.options(
        joinedload(CleaningRecord.room).joinedload(Room.room_type),
        joinedload(CleaningRecord.cleaner),
    )

    if room_id:
        query = query.filter(CleaningRecord.room_id == room_id)
    if cleaner_id:
        query = query.filter(CleaningRecord.cleaner_id == cleaner_id)
    if start_date:
        query = query.filter(CleaningRecord.start_time >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
        query = query.filter(CleaningRecord.start_time < end_dt)

    query = query.order_by(CleaningRecord.start_time.desc())

    page = request.args.get('page', 1, type=int)
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    for record in pagination.items:
        record.duration = _format_duration(record.start_time, record.end_time)
        if record.checklist_json:
            try:
                items = json.loads(record.checklist_json)
                record.checklist_items = items
                record.checklist_checked = sum(1 for i in items if i.get('checked'))
                record.checklist_total = len(items)
            except (json.JSONDecodeError, TypeError):
                record.checklist_items = None
                record.checklist_checked = 0
                record.checklist_total = 0
        else:
            record.checklist_items = None

    filters = {
        'room_id': room_id,
        'cleaner_id': cleaner_id,
        'start_date': start_date,
        'end_date': end_date,
    }

    return render_template(
        'limpiezas.html',
        records=pagination.items,
        pagination=pagination,
        rooms=Room.query.all(),
        cleaners=Cleaner.query.all(),
        filters=filters,
    )


@bp.route('/exportar_excel')
@admin_required
def exportar_excel():
    room_id = request.args.get('room_id', '')
    cleaner_id = request.args.get('cleaner_id', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    query = CleaningRecord.query
    if room_id:
        query = query.filter(CleaningRecord.room_id == room_id)
    if cleaner_id:
        query = query.filter(CleaningRecord.cleaner_id == cleaner_id)
    if start_date:
        query = query.filter(CleaningRecord.start_time >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
        query = query.filter(CleaningRecord.start_time < end_dt)

    records = query.order_by(CleaningRecord.start_time.desc()).all()

    data = [{
        'Limpiador': record.cleaner.name if record.cleaner else 'Sin asignar',
        'Habitación': str(record.room.number) if record.room else 'Sin asignar',
        'Descripción': record.room.description if record.room else 'Sin descripción',
        'Fecha de Inicio': record.start_time.strftime('%d/%m/%Y') if record.start_time else 'N/A',
        'Hora Inicio': record.start_time.strftime('%H:%M') if record.start_time else 'N/A',
        'Duración': _format_duration(record.start_time, record.end_time),
    } for record in records]

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='Registros de Limpieza', index=False)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name='registros_limpieza.xlsx',
    )


@bp.route('/ultima-limpieza')
@admin_required
def ultima_limpieza():
    rooms = Room.query.order_by(Room.number).all()
    now = datetime.now()
    last_cleaning_info = []

    for room in rooms:
        last_record = (
            CleaningRecord.query
            .filter_by(room_id=room.id)
            .filter(CleaningRecord.end_time.isnot(None))
            .order_by(CleaningRecord.end_time.desc())
            .first()
        )
        if last_record:
            hours_since = (now - last_record.end_time).total_seconds() / 3600
            last_cleaning_info.append({
                'room_number': room.number,
                'room_description': room.description,
                'last_cleaned_date': last_record.end_time.strftime('%d/%m/%Y'),
                'last_cleaned_time': last_record.end_time.strftime('%H:%M'),
                'duration': _format_duration(last_record.start_time, last_record.end_time),
                'cleaner': last_record.cleaner.name if last_record.cleaner else 'Desconocido',
                'hours_since': hours_since,
            })
        else:
            last_cleaning_info.append({
                'room_number': room.number,
                'room_description': room.description,
                'last_cleaned_date': 'Nunca',
                'last_cleaned_time': '',
                'duration': '',
                'cleaner': '',
                'hours_since': None,
            })

    return render_template('ultima_limpieza.html', last_cleaning_info=last_cleaning_info)


# ── ADMIN – HELP & ANALYTICS ──────────────────────────────────────────────

@bp.route('/admin/help')
@admin_required
def admin_help():
    return render_template('admin_help.html')


@bp.route('/admin/analytics')
@admin_required
def admin_analytics():
    days = request.args.get('days', 7, type=int)
    if days not in (7, 14, 30, 90):
        days = 7
    desde = datetime.now() - timedelta(days=days)

    # 1. Care records in period (completed only)
    care_records = CareRecord.query.options(
        joinedload(CareRecord.care_types),
        joinedload(CareRecord.care_type),
        joinedload(CareRecord.resident),
        joinedload(CareRecord.worker),
    ).filter(
        CareRecord.start_time >= desde,
        CareRecord.end_time.isnot(None),
    ).all()

    # 2. Cleaning records in period
    cleaning_records = CleaningRecord.query.options(
        joinedload(CleaningRecord.cleaner),
        joinedload(CleaningRecord.room),
    ).filter(
        CleaningRecord.start_time >= desde,
        CleaningRecord.end_time.isnot(None),
    ).all()

    # 3. Vital sign readings in period
    vital_readings = VitalSignReading.query.options(
        joinedload(VitalSignReading.vital_sign_type),
        joinedload(VitalSignReading.care_record).joinedload(CareRecord.resident),
    ).filter(
        VitalSignReading.recorded_at >= desde,
    ).order_by(VitalSignReading.recorded_at.desc()).all()

    # === Compute analytics ===
    from collections import defaultdict, Counter

    # Care type frequency
    care_type_counter = Counter()
    for c in care_records:
        for ct in c.care_types:
            care_type_counter[ct.name] += 1
        if not c.care_types and c.care_type:
            care_type_counter[c.care_type.name] += 1
    top_care_types = care_type_counter.most_common(10)

    # Worker productivity (care sessions)
    worker_care_count = Counter()
    worker_clean_count = Counter()
    for c in care_records:
        if c.worker:
            worker_care_count[c.worker.name] += 1
    for c in cleaning_records:
        if c.cleaner:
            worker_clean_count[c.cleaner.name] += 1
    all_workers = set(list(worker_care_count.keys()) + list(worker_clean_count.keys()))
    worker_stats = sorted([{
        'name': w,
        'care': worker_care_count.get(w, 0),
        'cleaning': worker_clean_count.get(w, 0),
        'total': worker_care_count.get(w, 0) + worker_clean_count.get(w, 0),
    } for w in all_workers], key=lambda x: -x['total'])

    # Average care duration by type
    duration_by_type = defaultdict(list)
    for c in care_records:
        dur = c.calculate_duration()
        if dur:
            for ct in c.care_types:
                duration_by_type[ct.name].append(dur)
    avg_duration = sorted([{
        'type': name,
        'avg_min': round(sum(durs) / len(durs) / 60, 1),
        'count': len(durs),
    } for name, durs in duration_by_type.items()], key=lambda x: -x['count'])

    # Residents with most care sessions
    resident_care = Counter()
    for c in care_records:
        if c.resident:
            resident_care[c.resident.name] += 1
    top_residents = resident_care.most_common(10)

    # Care per day (for chart)
    care_per_day = Counter()
    clean_per_day = Counter()
    for c in care_records:
        care_per_day[c.start_time.strftime('%Y-%m-%d')] += 1
    for c in cleaning_records:
        clean_per_day[c.start_time.strftime('%Y-%m-%d')] += 1

    # Abnormal vital signs
    abnormal_vitals = []
    for r in vital_readings[:100]:
        vst = r.vital_sign_type
        is_low = vst.min_value is not None and r.value < vst.min_value
        is_high = vst.max_value is not None and r.value > vst.max_value
        if is_low or is_high:
            abnormal_vitals.append({
                'resident': r.care_record.resident.name if r.care_record.resident else '?',
                'type': vst.name,
                'value': r.value,
                'unit': vst.unit,
                'min': vst.min_value,
                'max': vst.max_value,
                'date': r.recorded_at.strftime('%d/%m/%Y %H:%M'),
                'alert': 'alta' if is_high else 'baja',
            })

    return render_template('admin_analytics.html',
        days=days,
        total_care=len(care_records),
        total_cleaning=len(cleaning_records),
        total_vitals=len(vital_readings),
        top_care_types=top_care_types,
        worker_stats=worker_stats,
        avg_duration=avg_duration,
        top_residents=top_residents,
        abnormal_vitals=abnormal_vitals[:20],
        care_per_day=json.dumps(dict(care_per_day)),
        clean_per_day=json.dumps(dict(clean_per_day)),
    )


@bp.route('/admin/performance')
@admin_required
def admin_performance():
    """Performance metrics dashboard: SLAs, checklist completion, worker productivity."""
    from collections import defaultdict, Counter
    from datetime import date

    days = request.args.get('days', 30, type=int)
    if days not in (7, 14, 30, 90):
        days = 30
    desde = datetime.now() - timedelta(days=days)

    # ── 1. Cleaning records in period ────────────────────────────────────────
    cleaning_records = CleaningRecord.query.options(
        joinedload(CleaningRecord.cleaner),
        joinedload(CleaningRecord.room).joinedload(Room.room_type),
    ).filter(
        CleaningRecord.start_time >= desde,
        CleaningRecord.end_time.isnot(None),
    ).all()

    # ── 2. SLA: % rooms cleaned within expected frequency ────────────────────
    stats = _compute_cleaning_stats(days)
    avg_per_room = stats['avg_per_room']

    # Current room urgency status
    all_rooms = Room.query.options(joinedload(Room.room_type), joinedload(Room.floor)).all()
    occupied = {r.room_number for r in Resident.query.filter_by(active=True).all() if r.room_number}

    cutoff_freq = datetime.now() - timedelta(days=90)
    freq_records = CleaningRecord.query.filter(
        CleaningRecord.end_time.isnot(None), CleaningRecord.start_time >= cutoff_freq,
    ).all()
    room_clean_count = {}
    room_last_cleaned = {}
    for rec in freq_records:
        room_clean_count[rec.room_id] = room_clean_count.get(rec.room_id, 0) + 1
        if rec.room_id not in room_last_cleaned or rec.start_time > room_last_cleaned[rec.room_id]:
            room_last_cleaned[rec.room_id] = rec.start_time

    now = datetime.now()
    sla_on_time = 0
    sla_overdue = 0
    overdue_rooms = []
    for room in all_rooms:
        is_res = room.room_type and 'residen' in room.room_type.name.lower()
        is_occ = room.number in occupied
        urgency, days_since, freq = _calculate_room_urgency(
            room.id, room_clean_count, room_last_cleaned, now, is_res, is_occ)
        if urgency < 1:
            sla_on_time += 1
        else:
            sla_overdue += 1
            if urgency >= 1.5:
                overdue_rooms.append({
                    'number': room.number,
                    'floor': room.floor.name if room.floor else '',
                    'type': room.room_type.name if room.room_type else '',
                    'days_since': days_since,
                    'frequency': round(freq, 1),
                    'urgency': round(urgency, 2),
                })
    sla_total = sla_on_time + sla_overdue
    sla_pct = round(sla_on_time / sla_total * 100) if sla_total > 0 else 100
    overdue_rooms.sort(key=lambda x: -x['urgency'])

    # ── 3. Checklist completion rate ─────────────────────────────────────────
    checklist_total = 0
    checklist_complete = 0
    checklist_partial = 0
    for rec in cleaning_records:
        if rec.checklist_json:
            try:
                items = json.loads(rec.checklist_json)
                if items:
                    checklist_total += 1
                    checked = sum(1 for i in items if i.get('checked'))
                    if checked == len(items):
                        checklist_complete += 1
                    elif checked > 0:
                        checklist_partial += 1
            except (json.JSONDecodeError, TypeError):
                pass
    checklist_pct = round(checklist_complete / checklist_total * 100) if checklist_total > 0 else 0
    checklist_with_data_pct = round(checklist_total / len(cleaning_records) * 100) if cleaning_records else 0

    # ── 4. Worker productivity ───────────────────────────────────────────────
    worker_metrics = defaultdict(lambda: {
        'cleaning_count': 0, 'care_count': 0,
        'cleaning_minutes': 0, 'care_minutes': 0,
        'shift_minutes': 0, 'days_worked': set(),
    })

    for rec in cleaning_records:
        if rec.cleaner:
            m = worker_metrics[rec.cleaner.name]
            m['cleaning_count'] += 1
            dur = rec.calculate_duration()
            if dur:
                m['cleaning_minutes'] += dur / 60
            m['days_worked'].add(rec.start_time.date())

    care_records = CareRecord.query.options(
        joinedload(CareRecord.worker),
    ).filter(
        CareRecord.start_time >= desde,
        CareRecord.end_time.isnot(None),
    ).all()

    for rec in care_records:
        if rec.worker:
            m = worker_metrics[rec.worker.name]
            m['care_count'] += 1
            dur = rec.calculate_duration()
            if dur:
                m['care_minutes'] += dur / 60
            m['days_worked'].add(rec.start_time.date())

    # Get shift data for productivity ratio
    shift_assignments = ShiftAssignment.query.options(
        joinedload(ShiftAssignment.cleaner),
        joinedload(ShiftAssignment.shift_type),
    ).filter(
        ShiftAssignment.date >= desde.date(),
        ShiftAssignment.shift_type_id.isnot(None),
    ).all()

    for sa in shift_assignments:
        if sa.cleaner and sa.shift_type:
            m = worker_metrics[sa.cleaner.name]
            st = sa.shift_type
            start_dt = datetime.combine(sa.date, st.start_time)
            end_dt = datetime.combine(sa.date, st.end_time)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            shift_mins = ((end_dt - start_dt).total_seconds() / 60) - (st.breaks_minutes or 0)
            m['shift_minutes'] += shift_mins

    worker_table = []
    for name, m in worker_metrics.items():
        total_work = m['cleaning_minutes'] + m['care_minutes']
        productivity = round(total_work / m['shift_minutes'] * 100) if m['shift_minutes'] > 0 else 0
        avg_per_day = round(total_work / len(m['days_worked'])) if m['days_worked'] else 0
        worker_table.append({
            'name': name,
            'cleaning_count': m['cleaning_count'],
            'care_count': m['care_count'],
            'total_tasks': m['cleaning_count'] + m['care_count'],
            'work_hours': round(total_work / 60, 1),
            'shift_hours': round(m['shift_minutes'] / 60, 1),
            'productivity': min(productivity, 100),
            'days_worked': len(m['days_worked']),
            'avg_min_per_day': avg_per_day,
        })
    worker_table.sort(key=lambda x: -x['total_tasks'])

    # ── 5. Daily trends ──────────────────────────────────────────────────────
    clean_per_day = Counter()
    care_per_day = Counter()
    for rec in cleaning_records:
        clean_per_day[rec.start_time.strftime('%Y-%m-%d')] += 1
    for rec in care_records:
        care_per_day[rec.start_time.strftime('%Y-%m-%d')] += 1

    # ── 6. Average cleaning time vs target ───────────────────────────────────
    targets = {t.room_type_id: t.target_minutes for t in CleaningTargetTime.query.all()}
    type_stats = defaultdict(lambda: {'times': [], 'target': None, 'name': ''})
    for rec in cleaning_records:
        if rec.room and rec.room.room_type:
            dur = rec.calculate_duration()
            if dur and 60 < dur < 7200:
                rt = rec.room.room_type
                ts = type_stats[rt.id]
                ts['times'].append(dur / 60)
                ts['target'] = targets.get(rt.id)
                ts['name'] = rt.name

    room_type_perf = []
    for rt_id, ts in type_stats.items():
        avg = round(sum(ts['times']) / len(ts['times']), 1)
        target = ts['target'] or 15
        variance = round((avg - target) / target * 100)
        room_type_perf.append({
            'name': ts['name'],
            'avg_minutes': avg,
            'target': target,
            'count': len(ts['times']),
            'variance': variance,
        })
    room_type_perf.sort(key=lambda x: -abs(x['variance']))

    return render_template('admin_performance.html',
        days=days,
        sla_pct=sla_pct, sla_on_time=sla_on_time, sla_overdue=sla_overdue,
        overdue_rooms=overdue_rooms[:15],
        checklist_pct=checklist_pct, checklist_total=checklist_total,
        checklist_complete=checklist_complete, checklist_partial=checklist_partial,
        checklist_with_data_pct=checklist_with_data_pct,
        total_cleanings=len(cleaning_records), total_cares=len(care_records),
        worker_table=worker_table,
        room_type_perf=room_type_perf,
        clean_per_day=json.dumps(dict(clean_per_day)),
        care_per_day=json.dumps(dict(care_per_day)),
    )


@bp.route('/api/worker-performance/ai-analysis', methods=['POST'])
@admin_required
def ai_worker_performance():
    """AI analysis of worker performance data."""
    from ..blueprints.assessments import _call_claude
    from ..models import (CareRecord, CleaningRecord, TrainingCompletion,
                          ShiftAssignment, Absence)

    days = 30
    cutoff = datetime.now() - timedelta(days=days)

    workers = Cleaner.query.filter_by(active=True, is_admin=False).order_by(Cleaner.name).all()
    lines = [f"ANALISIS RENDIMENT TREBALLADORS — Ultims {days} dies\n"]

    for w in workers:
        cares = CareRecord.query.filter(
            CareRecord.worker_id == w.id, CareRecord.start_time >= cutoff,
            CareRecord.end_time.isnot(None)).all()
        cleans = CleaningRecord.query.filter(
            CleaningRecord.cleaner_id == w.id, CleaningRecord.start_time >= cutoff,
            CleaningRecord.end_time.isnot(None)).all()
        shifts = ShiftAssignment.query.filter(
            ShiftAssignment.cleaner_id == w.id,
            ShiftAssignment.date >= cutoff.date()).count()
        absences = Absence.query.filter(
            Absence.cleaner_id == w.id,
            Absence.start_date >= cutoff.date()).count()
        trainings = TrainingCompletion.query.filter_by(
            cleaner_id=w.id, passed=True).count()

        care_durations = [c.calculate_duration() for c in cares if c.calculate_duration()]
        avg_care = round(sum(care_durations) / len(care_durations) / 60, 1) if care_durations else 0
        clean_durations = [c.calculate_duration() for c in cleans if c.calculate_duration()]
        avg_clean = round(sum(clean_durations) / len(clean_durations) / 60, 1) if clean_durations else 0

        lines.append(f"- {w.name} (rol: {w.role}): {len(cares)} atencions (mitja {avg_care}min), "
                     f"{len(cleans)} neteges (mitja {avg_clean}min), {shifts} torns, "
                     f"{absences} absencies, {trainings} formacions aprovades")

    system = (
        "Eres un gestor de recursos humanos de una residencia geriatrica. "
        "Analiza el rendimiento de los trabajadores y genera un informe breve en HTML. "
        "Destaca: trabajadores mas productivos, los que tienen tiempos por encima de la media, "
        "patrones de absentismo, y recomendaciones de mejora. "
        "Se constructivo, no acusatorio. Formato: HTML con h3 y ul/li. Sin emojis."
    )

    try:
        html = _call_claude(system, '\n'.join(lines))
        html = html.strip()
        if html.startswith('```'):
            html = html.split('\n', 1)[1] if '\n' in html else html[3:]
        if html.endswith('```'):
            html = html.rsplit('```', 1)[0]
        return jsonify({'html': html.strip()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/admin/audit')
@admin_required
def admin_audit():
    """View audit trail."""
    page = request.args.get('page', 1, type=int)
    table_filter = request.args.get('table', '')
    action_filter = request.args.get('action', '')

    query = AuditLog.query.options(joinedload(AuditLog.user))

    if table_filter:
        query = query.filter(AuditLog.table_name == table_filter)
    if action_filter:
        query = query.filter(AuditLog.action == action_filter)

    pagination = query.order_by(AuditLog.created_at.desc()).paginate(
        page=page, per_page=50, error_out=False)

    tables = db.session.query(AuditLog.table_name).distinct().order_by(AuditLog.table_name).all()
    table_names = [t[0] for t in tables]

    return render_template('admin_audit.html',
        logs=pagination.items, pagination=pagination,
        table_filter=table_filter, action_filter=action_filter,
        table_names=table_names,
    )
