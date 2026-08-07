from __future__ import annotations
from . import app, db, limiter
from .models import (Cleaner, Room, CleaningRecord, Floor, RoomType, Resident,
                      CareType, CareRecord, ResidentGroup, cleaner_groups,
                      WorkerSelfie, LegalDocument, DocumentSignature,
                      TrainingPill, TrainingQuestion, TrainingCompletion,
                      ChecklistItem, ResidentDocument,
                      VitalSignType, VitalSignReading, AppSetting,
                      ShiftType, ShiftAssignment,
                      RotationPattern, RotationPatternDay, WorkerShiftConfig,
                      AbsenceType, Absence, ShiftCoverageRequirement,
                      CleaningZoneAssignment, CleaningTargetTime)
from flask import request, jsonify, render_template, redirect, url_for, flash, send_file, send_from_directory, abort
from flask_login import login_user, logout_user, login_required, current_user
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from werkzeug.utils import secure_filename as _secure_filename

ALLOWED_DOC_EXTENSIONS = {'pdf', 'txt', 'csv', 'doc', 'docx', 'jpg', 'jpeg', 'png', 'webp'}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}

from functools import wraps
def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _allowed_file(filename: str, allowed: set) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed
from datetime import datetime, timedelta, date, time as dt_time
from sqlalchemy.orm import joinedload, subqueryload
from sqlalchemy.exc import IntegrityError
import pandas as pd
from io import BytesIO
import time
import click
import base64
import os
import hashlib
import json
import random


def _verify_worker_id(worker_id):
    """Verify that the supplied worker_id matches the JWT identity. Returns the worker_id if valid."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker or worker.id != worker_id:
        return None
    return worker_id


# ── CLI ───────────────────────────────────────────────────────────────────────

@app.cli.command('create-admin')
@click.argument('username')
def create_admin(username: str) -> None:
    """Otorga permisos de administrador a un usuario existente.

    Uso: flask create-admin <username>
    """
    cleaner = Cleaner.query.filter_by(username=username).first()
    if not cleaner:
        print(f'Usuario "{username}" no encontrado.')
        return
    cleaner.is_admin = True
    db.session.commit()
    print(f'"{username}" ahora es administrador.')


@app.cli.command('init-admin')
@click.argument('username')
@click.argument('password')
@click.option('--name', default=None, help='Nombre visible del administrador')
def init_admin(username: str, password: str, name: str | None) -> None:
    """Crea un usuario administrador. Si ya existe, actualiza su contraseña y permisos.

    Uso: flask init-admin <username> <password>
    """
    cleaner = Cleaner.query.filter_by(username=username).first()
    if cleaner:
        cleaner.set_password(password)
        cleaner.is_admin = True
        db.session.commit()
        print(f'Usuario "{username}" actualizado como administrador.')
    else:
        cleaner = Cleaner(username=username, name=name or username, is_admin=True)
        cleaner.set_password(password)
        db.session.add(cleaner)
        db.session.commit()
        print(f'Administrador "{username}" creado correctamente.')


# ── HELPER ───────────────────────────────────────────────────────────────────

def _format_duration(start_time: datetime | None, end_time: datetime | None) -> str:
    if start_time and end_time:
        seconds = int((end_time - start_time).total_seconds())
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f'{hours:02d}:{minutes:02d}:{secs:02d}'
    return 'N/A'


# ── WEB AUTH ─────────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
@limiter.limit("10/minute", methods=["POST"])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = Cleaner.query.filter_by(username=username).first()

        if user and user.check_password(password) and user.is_admin:
            login_user(user)
            next_page = request.args.get('next', '')
            if not next_page or not next_page.startswith('/') or next_page.startswith('//'):
                next_page = url_for('index')
            return redirect(next_page)

        flash('Credenciales incorrectas o sin permisos de administrador.', 'danger')

    return render_template('login.html')


@app.route('/admin/logout', methods=['POST'])
@login_required
def admin_logout():
    logout_user()
    flash('Sesión cerrada correctamente.', 'success')
    return redirect(url_for('admin_login'))


# ── HOME ─────────────────────────────────────────────────────────────────────

@app.route('/')
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
        s.end_time = s.start_time + timedelta(hours=1)  # Mark as 1h duration
    for s in stale_cares:
        s.end_time = s.start_time + timedelta(hours=1)
    if stale_count:
        db.session.commit()

    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    hoy_inicio = datetime.combine(today, datetime.min.time())
    hoy_fin = datetime.combine(tomorrow, datetime.min.time())

    limpiezas_hoy = CleaningRecord.query.filter(
        CleaningRecord.start_time >= hoy_inicio,
        CleaningRecord.start_time < hoy_fin,
    ).count()

    en_curso = CleaningRecord.query.filter(CleaningRecord.end_time.is_(None)).count()

    # Rooms not cleaned TODAY (more useful than "never cleaned")
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

    # Residence overview stats
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

    # Vital signs alerts today
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
    )


# ── API – APP MÓVIL (sin autenticación web, usan JWT) ────────────────────────

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10/minute", methods=["POST"])
def login():
    """Endpoint de autenticación para la app Android – devuelve JWT."""
    if request.method == 'GET':
        return redirect(url_for('admin_login'))

    username = request.form.get('username') or (request.json or {}).get('username')
    password = request.form.get('password') or (request.json or {}).get('password')

    user = Cleaner.query.filter_by(username=username).first()
    if user and user.check_password(password):
        access_token = create_access_token(identity=username, expires_delta=timedelta(hours=12))
        return jsonify(access_token=access_token, id_cleaner=user.id, cleaner_name=user.name, role=user.role), 200

    return jsonify({'error': 'Credenciales incorrectas'}), 401


@app.route('/start_cleaning', methods=['POST'])
@jwt_required()
def start_cleaning():
    data = request.json or {}
    cleaner_id = data.get('cleaner_id')
    room_number = data.get('room_id')
    if not cleaner_id or not room_number:
        return jsonify({'error': 'Campos requeridos: cleaner_id, room_id'}), 400

    room = Room.query.filter_by(number=room_number).first()
    if not room:
        return jsonify({'error': 'Habitación no encontrada'}), 404

    active_cleaning = CleaningRecord.query.filter_by(
        cleaner_id=cleaner_id, room_id=room.id, end_time=None
    ).first()

    if active_cleaning:
        active_cleaning.end_time = datetime.now()
        db.session.commit()
        return jsonify({
            'message': f'Limpieza {active_cleaning.id} finalizada en habitación {room_number}.'
        }), 200

    # Check single session restriction
    if AppSetting.get('single_session', 'false') == 'true':
        any_active = CleaningRecord.query.filter_by(cleaner_id=cleaner_id, end_time=None).first() or \
                     CareRecord.query.filter_by(worker_id=cleaner_id, end_time=None).first()
        if any_active:
            return jsonify({'error': 'Ya tienes una sesión activa. Finalízala antes de iniciar otra.'}), 409

    new_record = CleaningRecord(cleaner_id=cleaner_id, room_id=room.id, start_time=datetime.now())
    db.session.add(new_record)
    db.session.commit()
    return jsonify({
        'message': f'Limpieza {new_record.id} iniciada en habitación {room_number}.',
        'record_id': new_record.id,
    }), 200


@app.route('/end_cleaning', methods=['POST'])
@jwt_required()
def end_cleaning():
    data = request.json or {}
    record_id = data.get('record_id')
    if not record_id:
        return jsonify({'error': 'Campo requerido: record_id'}), 400
    record = db.session.get(CleaningRecord, record_id)
    if not record or record.end_time:
        return jsonify({'error': 'Registro no válido o limpieza ya finalizada.'}), 400
    record.end_time = datetime.now()
    db.session.commit()
    return jsonify({'message': 'Limpieza finalizada.', 'duration': record.calculate_duration()}), 200


@app.route('/check_cleaning', methods=['GET'])
@jwt_required()
def check_cleaning():
    cleaner_id = request.args.get('cleaner_id')
    if not cleaner_id:
        return jsonify({'error': 'Falta el ID del limpiador.'}), 400
    record = CleaningRecord.query.filter_by(cleaner_id=cleaner_id, end_time=None).first()
    if record:
        return jsonify({'room_id': record.room_id}), 200
    return jsonify({'message': 'No hay limpiezas en curso.'}), 200


@app.route('/cleaning_summary/<int:cleaner_id>', methods=['GET'])
@jwt_required()
def cleaning_summary(cleaner_id: int):
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    records = CleaningRecord.query.filter(
        CleaningRecord.cleaner_id == cleaner_id,
        CleaningRecord.start_time >= datetime.combine(today, datetime.min.time()),
        CleaningRecord.start_time < datetime.combine(tomorrow, datetime.min.time()),
    ).all()

    summary = []
    for record in records:
        if record.end_time:
            secs = record.calculate_duration()
            summary.append(f'{record.room.description}, {time.strftime("%H:%M:%S", time.gmtime(secs))}')
        else:
            summary.append(str(record.room.description))
    return jsonify(summary)


@app.route('/api/registros-limpieza', methods=['GET'])
@login_required
def api_registros_limpieza():
    records = CleaningRecord.current_year_records().all()
    data = [{
        'Limpiador': record.cleaner.name if record.cleaner else 'Desconocido',
        'Habitación': record.room.number if record.room else 'No asignado',
        'Descripción': record.room.description if record.room else 'No disponible',
        'Tipo de Espacio': record.room.room_type.name if record.room and record.room.room_type else 'Tipo desconocido',
        'Fecha de Inicio': record.start_time.strftime('%Y-%m-%d') if record.start_time else None,
        'Hora de Inicio': record.start_time.strftime('%H:%M') if record.start_time else None,
        'Fecha de FIN': record.end_time.strftime('%Y-%m-%d') if record.end_time else None,
        'Hora de FIN': record.end_time.strftime('%H:%M') if record.end_time else None,
        'Duración': _format_duration(record.start_time, record.end_time),
    } for record in records]
    return jsonify(data)


# ── WEB ADMIN – EMPLEADOS ─────────────────────────────────────────────────────

@app.route('/manage_workers')
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


@app.route('/cleaners/add_edit', methods=['POST'])
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

    return redirect(url_for('manage_workers'))


@app.route('/cleaners/delete/<int:id>', methods=['POST'])
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
    return redirect(url_for('manage_workers'))


@app.route('/cleaners/update-groups', methods=['POST'])
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


@app.route('/cleaners/update-active', methods=['POST'])
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


# ── WEB ADMIN – ZONAS DE LIMPIEZA ────────────────────────────────────────────

@app.route('/zonas-limpieza')
@admin_required
def manage_cleaning_zones():
    rooms = Room.query.all()
    floors = Floor.query.all()
    room_types = RoomType.query.all()
    return render_template(
        'manage_cleaning_zones.html',
        rooms=rooms, floors=floors, room_types=room_types, form_data={}
    )


@app.route('/rooms/add_edit', methods=['POST'])
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

    return redirect(url_for('manage_cleaning_zones'))


@app.route('/rooms/delete/<int:id>', methods=['POST'])
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
    return redirect(url_for('manage_cleaning_zones'))


# ── WEB ADMIN – TIPOS DE ESPACIO ─────────────────────────────────────────────

@app.route('/manage_room_types')
@admin_required
def manage_room_types():
    room_types = RoomType.query.all()
    return render_template('manage_room_types.html', room_types=room_types)


@app.route('/room_types/add_edit', methods=['POST'])
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
    return redirect(url_for('manage_room_types'))


@app.route('/room_types/delete/<int:id>', methods=['POST'])
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
    return redirect(url_for('manage_room_types'))


# ── WEB ADMIN – PLANTAS ───────────────────────────────────────────────────────

@app.route('/manage_floors')
@admin_required
def manage_floors():
    floors = Floor.query.all()
    return render_template('manage_floors.html', floors=floors)


@app.route('/floors/add_edit', methods=['POST'])
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
    return redirect(url_for('manage_floors'))


@app.route('/floors/delete/<int:id>', methods=['POST'])
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
    return redirect(url_for('manage_floors'))


# ── WEB ADMIN – REGISTROS DE LIMPIEZA ────────────────────────────────────────

@app.route('/admin/close-session/<mode>/<int:record_id>', methods=['POST'])
@admin_required
def admin_close_session(mode: str, record_id: int):
    now = datetime.now()
    if mode == 'cleaning':
        rec = db.session.get(CleaningRecord, record_id)
        if rec and not rec.end_time:
            rec.end_time = now
            db.session.commit()
            flash(f'Sesión de limpieza cerrada (Hab. {rec.room.number if rec.room else record_id}).', 'success')
        return redirect(request.referrer or url_for('registros_limpieza'))
    elif mode == 'care':
        rec = db.session.get(CareRecord, record_id)
        if rec and not rec.end_time:
            rec.end_time = now
            db.session.commit()
            flash(f'Sesión de atención cerrada ({rec.resident.name if rec.resident else record_id}).', 'success')
        return redirect(request.referrer or url_for('registros_atencion'))
    abort(400)


@app.route('/registros-limpieza')
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


@app.route('/exportar_excel')
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


@app.route('/exportar_atenciones_excel')
@admin_required
def exportar_atenciones_excel():
    worker_id = request.args.get('worker_id', '')
    resident_id = request.args.get('resident_id', '')
    care_type_id = request.args.get('care_type_id', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    query = CareRecord.query.options(
        joinedload(CareRecord.resident),
        joinedload(CareRecord.worker),
        joinedload(CareRecord.care_types),
        joinedload(CareRecord.care_type),
        joinedload(CareRecord.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
    )
    if worker_id:
        query = query.filter(CareRecord.worker_id == worker_id)
    if resident_id:
        query = query.filter(CareRecord.resident_id == resident_id)
    if care_type_id:
        query = query.filter(CareRecord.care_type_id == care_type_id)
    if start_date:
        query = query.filter(CareRecord.start_time >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
        query = query.filter(CareRecord.start_time < end_dt)

    records = query.order_by(CareRecord.start_time.desc()).all()

    data = []
    for r in records:
        types = ', '.join(ct.name for ct in r.care_types) if r.care_types else (r.care_type.name if r.care_type else '')
        vitals = '; '.join(f'{v.vital_sign_type.name}: {v.value} {v.vital_sign_type.unit}' for v in r.vital_sign_readings) if r.vital_sign_readings else ''
        data.append({
            'Residente': r.resident.name if r.resident else 'Sin asignar',
            'Tipo de atención': types,
            'Trabajador': r.worker.name if r.worker else 'Sin asignar',
            'Fecha': r.start_time.strftime('%d/%m/%Y') if r.start_time else 'N/A',
            'Hora': r.start_time.strftime('%H:%M') if r.start_time else 'N/A',
            'Duración': _format_duration(r.start_time, r.end_time),
            'Constantes vitales': vitals,
        })

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='Registros de Atención', index=False)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name='registros_atencion.xlsx',
    )


@app.route('/ultima-limpieza')
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


# ── CLI – TIPOS DE ATENCIÓN ───────────────────────────────────────────────────

@app.cli.command('close-orphan-sessions')
@click.option('--before', default=None, help='Fecha límite YYYY-MM-DD (cierra sesiones anteriores a esta fecha)')
@click.option('--dry-run', is_flag=True, help='Solo mostrar, no modificar')
def close_orphan_sessions(before: str | None, dry_run: bool) -> None:
    """Cierra sesiones de limpieza sin finalizar (end_time=None).

    Uso: flask close-orphan-sessions --before 2026-04-25
         flask close-orphan-sessions --dry-run
    """
    from datetime import date
    query = CleaningRecord.query.filter(CleaningRecord.end_time.is_(None))
    if before:
        cutoff = datetime.strptime(before, '%Y-%m-%d')
        query = query.filter(CleaningRecord.start_time < cutoff)
    records = query.all()
    print(f'Sesiones abiertas encontradas: {len(records)}')
    if dry_run:
        for r in records:
            print(f'  id={r.id} cleaner_id={r.cleaner_id} start={r.start_time}')
        return
    for r in records:
        db.session.delete(r)
    db.session.commit()
    print(f'{len(records)} sesiones eliminadas.')


@app.cli.command('seed-care-types')
def seed_care_types() -> None:
    """Crea los tipos de atención por defecto.

    Uso: flask seed-care-types
    """
    defaults = [
        'Aseo e higiene', 'Medicación', 'Fisioterapia',
        'Comida', 'Compañía', 'Cambio de postura', 'Cura / Heridas', 'Otro',
    ]
    created = 0
    for name in defaults:
        if not CareType.query.filter_by(name=name).first():
            db.session.add(CareType(name=name))
            created += 1
    db.session.commit()
    print(f'{created} tipo(s) de atención creados.')


# ── WORKER WEBAPP ─────────────────────────────────────────────────────────────

@app.route('/worker')
def worker():
    return render_template('worker.html')


@app.route('/worker/manifest.json')
def worker_manifest():
    return jsonify({
        'name': 'La Vila Gran',
        'short_name': 'La Vila Gran',
        'description': 'Registro de limpiezas y atenciones',
        'start_url': '/worker',
        'display': 'standalone',
        'background_color': '#ffffff',
        'theme_color': '#0069d9',
        'orientation': 'portrait',
        'icons': [{
            'src': url_for('static', filename='logoLaVilaGranBanner.png'),
            'sizes': '192x192',
            'type': 'image/png',
            'purpose': 'any maskable',
        }],
    })


# ── API – WORKER (JWT) ────────────────────────────────────────────────────────

@app.route('/api/chat', methods=['POST'])
@jwt_required()
def api_chat():
    from .chatbot import chat
    api_key = app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'Chatbot no configurado (falta ANTHROPIC_API_KEY)'}), 503
    data = request.json or {}
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Mensaje vacío'}), 400
    try:
        response = chat(message, api_key)
        return jsonify({'response': response}), 200
    except Exception as e:
        app.logger.error(f'Chatbot error: {e}')
        return jsonify({'error': 'Error del chatbot: no se pudo procesar la consulta'}), 500


@app.route('/api/transcribe', methods=['POST'])
@jwt_required()
def api_transcribe():
    return _transcribe_audio()


@app.route('/admin/transcribe', methods=['POST'])
@admin_required
def admin_transcribe():
    return _transcribe_audio()


def _transcribe_audio():
    api_key = app.config.get('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'error': 'Transcripción no configurada (falta OPENAI_API_KEY)'}), 503
    audio_file = request.files.get('audio')
    if not audio_file:
        return jsonify({'error': 'No se recibió audio'}), 400
    try:
        import requests as req
        resp = req.post(
            'https://api.openai.com/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {api_key}'},
            files={'file': (audio_file.filename or 'audio.webm', audio_file.stream, audio_file.content_type or 'audio/webm')},
            data={'model': 'whisper-1', 'language': 'es'},
            timeout=30,
        )
        if resp.status_code != 200:
            app.logger.error(f'Whisper API error: {resp.status_code} {resp.text}')
            return jsonify({'error': 'Error en la transcripción'}), 500
        text = resp.json().get('text', '')
        return jsonify({'text': text}), 200
    except Exception as e:
        app.logger.error(f'Transcribe error: {e}')
        return jsonify({'error': 'Error en la transcripción'}), 500


@app.route('/admin/chat', methods=['POST'])
@admin_required
def admin_chat():
    from .chatbot import chat
    api_key = app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'Chatbot no configurado (falta ANTHROPIC_API_KEY)'}), 503
    data = request.json or {}
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Mensaje vacío'}), 400
    try:
        response = chat(message, api_key, is_admin=True)
        return jsonify({'response': response}), 200
    except Exception as e:
        app.logger.error(f'Chatbot error: {e}')
        return jsonify({'error': 'Error del chatbot: no se pudo procesar la consulta'}), 500


@app.route('/api/care-types')
@jwt_required()
def api_care_types():
    types = CareType.query.filter_by(parent_id=None, active=True).order_by(CareType.sort_order, CareType.name).all()

    def _ct_dict(ct):
        d = {
            'id': ct.id,
            'name': ct.name,
            'icon': ct.icon or '',
            'icon_url': f'/api/uploads/{ct.icon_path}' if ct.icon_path else None,
        }
        vital_fields = [{'id': vs.id, 'name': vs.name, 'unit': vs.unit,
                         'min_value': vs.min_value, 'max_value': vs.max_value,
                         'input_type': vs.input_type or 'number',
                         } for vs in (ct.vital_sign_types or []) if vs.active]
        if vital_fields:
            d['vital_fields'] = vital_fields
        return d

    return jsonify([{
        **_ct_dict(t),
        'children': [_ct_dict(c) for c in sorted(t.children, key=lambda x: (x.sort_order, x.name)) if c.active],
    } for t in types])


@app.route('/api/debug/record')
@admin_required
def debug_record():
    """Diagnóstico: comprueba un registro por ID. Solo admin. /api/debug/record?id=X&mode=cleaning"""
    record_id = request.args.get('id', type=int)
    mode = request.args.get('mode', 'cleaning')
    if not record_id:
        return jsonify({'error': 'Falta ?id=X'}), 400
    if mode == 'cleaning':
        r = db.session.get(CleaningRecord, record_id)
        if not r:
            return jsonify({'found': False, 'id': record_id}), 404
        return jsonify({'found': True, 'id': r.id, 'cleaner_id': r.cleaner_id,
                        'room_id': r.room_id, 'start_time': str(r.start_time),
                        'end_time': str(r.end_time)})
    if mode == 'care':
        r = db.session.get(CareRecord, record_id)
        if not r:
            return jsonify({'found': False, 'id': record_id}), 404
        return jsonify({'found': True, 'id': r.id, 'worker_id': r.worker_id,
                        'resident_id': r.resident_id, 'start_time': str(r.start_time),
                        'end_time': str(r.end_time)})
    return jsonify({'error': 'mode debe ser cleaning o care'}), 400


@app.route('/api/worker/active-sessions')
@jwt_required()
def worker_active_sessions():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify([]), 200

    sessions: list[dict] = []

    for c in CleaningRecord.query.filter_by(cleaner_id=worker_id, end_time=None).all():
        room = c.room
        sessions.append({
            'type': 'cleaning',
            'record_id': c.id,
            'start_time': c.start_time.isoformat(),
            'subject': f'Hab. {room.number}' if room else 'Habitación',
            'subject_sub': room.description or '' if room else '',
        })

    care_records = CareRecord.query.filter_by(worker_id=worker_id, end_time=None).all()

    # Group by start_time (truncated to second) to detect group care sessions
    from collections import defaultdict
    by_start = defaultdict(list)
    for c in care_records:
        key = c.start_time.replace(microsecond=0).isoformat() if c.start_time else None
        by_start[key].append(c)

    for key, records in by_start.items():
        group_key = key if len(records) > 1 else None
        for c in records:
            sub = ', '.join(ct.name for ct in c.care_types) if c.care_types else (c.care_type.name if c.care_type else '')
            r = c.resident
            sessions.append({
                'type': 'care',
                'record_id': c.id,
                'start_time': c.start_time.isoformat(),
                'subject': r.name if r else 'Residente',
                'subject_sub': sub,
                'photo_url': f'/api/uploads/{r.photo_path}' if r and r.photo_path else None,
                'group_key': group_key,
                'resident_id': r.id if r else None,
            })

    return jsonify(sessions), 200


@app.route('/api/rooms')
@jwt_required()
def api_rooms():
    today = datetime.now().date()
    hoy_inicio = datetime.combine(today, datetime.min.time())
    hoy_fin = datetime.combine(today + timedelta(days=1), datetime.min.time())

    # Single query: all completed cleanings today with cleaner eager-loaded
    all_cleanings = CleaningRecord.query.options(
        joinedload(CleaningRecord.cleaner),
    ).filter(
        CleaningRecord.start_time >= hoy_inicio,
        CleaningRecord.start_time < hoy_fin,
        CleaningRecord.end_time.isnot(None),
    ).order_by(CleaningRecord.start_time.desc()).all()

    # Group by room_id
    from collections import defaultdict
    cleanings_by_room: dict[int, list] = defaultdict(list)
    for c in all_cleanings:
        cleanings_by_room[c.room_id].append({
            'time': c.start_time.strftime('%H:%M'),
            'cleaner': c.cleaner.name if c.cleaner else '',
            'duration': _format_duration(c.start_time, c.end_time),
        })

    # Single query: all floors with rooms eager-loaded
    floors = Floor.query.order_by(Floor.name).all()
    result: list[dict] = []
    for floor in floors:
        rooms = Room.query.filter_by(floor_id=floor.id).order_by(Room.number).all()
        if rooms:
            rooms_data = []
            for r in rooms:
                ct = cleanings_by_room.get(r.id, [])
                rooms_data.append({
                    'id': r.id,
                    'number': r.number,
                    'description': r.description or '',
                    'cleaned_today': len(ct) > 0,
                    'cleaning_count_today': len(ct),
                    'cleaning_today': ct,
                })
            result.append({
                'id': floor.id,
                'name': floor.name,
                'rooms': rooms_data,
            })
    return jsonify({'floors': result}), 200


@app.route('/api/residents')
@jwt_required()
def api_residents():
    worker_id = request.args.get('worker_id', type=int)

    today = datetime.now().date()
    hoy_inicio = datetime.combine(today, datetime.min.time())
    hoy_fin = datetime.combine(today + timedelta(days=1), datetime.min.time())

    # Single query: all completed care records today with care_types eager-loaded
    all_care = CareRecord.query.options(
        db.joinedload(CareRecord.care_types),
        db.joinedload(CareRecord.care_type),
    ).filter(
        CareRecord.start_time >= hoy_inicio,
        CareRecord.start_time < hoy_fin,
        CareRecord.end_time.isnot(None),
    ).order_by(CareRecord.start_time.desc()).all()

    # Group by resident_id
    from collections import defaultdict
    care_by_resident: dict[int, list] = defaultdict(list)
    for c in all_care:
        types = ', '.join(ct.name for ct in c.care_types) if c.care_types else (c.care_type.name if c.care_type else '')
        care_by_resident[c.resident_id].append({
            'time': c.start_time.strftime('%H:%M'),
            'types': types,
            'duration': _format_duration(c.start_time, c.end_time),
        })

    def _resident_data(r):
        ct = care_by_resident.get(r.id, [])
        return {
            'id': r.id, 'name': r.name, 'nfc_code': r.nfc_code,
            'room_number': r.room_number or '',
            'has_photo': bool(r.photo_path),
            'has_info': bool(r.relevant_info),
            'photo_url': f'/api/uploads/{r.photo_path}' if r.photo_path else None,
            'has_care_today': len(ct) > 0,
            'care_count_today': len(ct),
            'care_today': ct,
        }

    # Determinar grupos propios del worker
    worker_group_ids: list[int] = []
    if worker_id:
        worker = db.session.get(Cleaner, worker_id)
        if worker and worker.groups:
            worker_group_ids = [g.id for g in worker.groups]

    groups = ResidentGroup.query.order_by(ResidentGroup.name).all()
    result: list[dict] = []
    for group in groups:
        residents = Resident.query.filter_by(group_id=group.id, active=True).order_by(Resident.name).all()
        if residents:
            result.append({
                'id': group.id,
                'name': group.name,
                'color': group.color,
                'is_mine': group.id in worker_group_ids,
                'residents': [_resident_data(r) for r in residents],
            })
    result.sort(key=lambda g: (not g['is_mine'], g['name']))

    ungrouped = Resident.query.filter_by(group_id=None, active=True).order_by(Resident.name).all()
    return jsonify({
        'groups': result,
        'ungrouped': [_resident_data(r) for r in ungrouped],
    }), 200


@app.route('/api/resident/<int:resident_id>/info')
@jwt_required()
def api_resident_info(resident_id):
    r = db.session.get(Resident, resident_id)
    if not r:
        return jsonify({'error': 'Residente no encontrado'}), 404
    return jsonify({
        'id': r.id,
        'name': r.name,
        'room_number': r.room_number or '',
        'relevant_info': r.relevant_info or '',
        'photo_url': f'/api/uploads/{r.photo_path}' if r.photo_path else None,
        'group_name': r.group.name if r.group else None,
        'group_color': r.group.color if r.group else None,
    }), 200


@app.route('/api/worker/active-session')
@jwt_required()
def worker_active_session():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'active': False}), 200

    cleaning = CleaningRecord.query.filter_by(cleaner_id=worker_id, end_time=None).first()
    if cleaning:
        room = cleaning.room
        return jsonify({
            'active': True,
            'type': 'cleaning',
            'record_id': cleaning.id,
            'start_time': cleaning.start_time.isoformat(),
            'subject': f'Hab. {room.number}' if room else 'Habitación',
            'subject_sub': room.description or '' if room else '',
        }), 200

    care = CareRecord.query.filter_by(worker_id=worker_id, end_time=None).first()
    if care:
        return jsonify({
            'active': True,
            'type': 'care',
            'record_id': care.id,
            'start_time': care.start_time.isoformat(),
            'subject': care.resident.name if care.resident else 'Residente',
            'subject_sub': care.care_type.name if care.care_type else '',
        }), 200

    return jsonify({'active': False}), 200


@app.route('/api/worker/today')
@jwt_required()
def worker_today():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'sessions': []}), 200

    today = datetime.now().date()
    hoy_inicio = datetime.combine(today, datetime.min.time())
    hoy_fin = datetime.combine(today + timedelta(days=1), datetime.min.time())

    sessions: list[dict] = []

    cleanings = (
        CleaningRecord.query
        .filter(
            CleaningRecord.cleaner_id == worker_id,
            CleaningRecord.start_time >= hoy_inicio,
            CleaningRecord.start_time < hoy_fin,
            CleaningRecord.end_time.isnot(None),
        )
        .order_by(CleaningRecord.start_time.desc())
        .all()
    )
    for c in cleanings:
        room = c.room
        sessions.append({
            'type': 'cleaning',
            'subject': f'Hab. {room.number}' if room else 'Habitación',
            'subject_sub': room.description or '' if room else '',
            'start_time': c.start_time.strftime('%H:%M'),
            'duration': _format_duration(c.start_time, c.end_time),
        })

    cares = (
        CareRecord.query
        .filter(
            CareRecord.worker_id == worker_id,
            CareRecord.start_time >= hoy_inicio,
            CareRecord.start_time < hoy_fin,
            CareRecord.end_time.isnot(None),
        )
        .order_by(CareRecord.start_time.desc())
        .all()
    )
    for c in cares:
        r = c.resident
        sub = ', '.join(ct.name for ct in c.care_types) if c.care_types else (c.care_type.name if c.care_type else '')
        sessions.append({
            'type': 'care',
            'subject': r.name if r else 'Residente',
            'subject_sub': sub,
            'start_time': c.start_time.strftime('%H:%M'),
            'duration': _format_duration(c.start_time, c.end_time),
            'photo_url': f'/api/uploads/{r.photo_path}' if r and r.photo_path else None,
            'care_today': [{
                'time': c.start_time.strftime('%H:%M'),
                'types': sub,
                'duration': _format_duration(c.start_time, c.end_time),
            }],
        })

    sessions.sort(key=lambda x: x['start_time'], reverse=True)
    return jsonify({'sessions': sessions}), 200


@app.route('/api/worker/my-groups')
@jwt_required()
def worker_my_groups():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'groups': []}), 200

    worker = Cleaner.query.options(
        subqueryload(Cleaner.groups).subqueryload(ResidentGroup.residents),
    ).get(worker_id)
    if not worker:
        return jsonify({'groups': []}), 200

    today = datetime.now().date()
    hoy_inicio = datetime.combine(today, datetime.min.time())
    hoy_fin = datetime.combine(today + timedelta(days=1), datetime.min.time())

    # Collect all resident IDs from worker's groups
    group_residents = {}
    all_resident_ids = []
    for group in worker.groups:
        active_residents = sorted([r for r in group.residents if r.active], key=lambda r: r.name)
        group_residents[group.id] = active_residents
        all_resident_ids.extend(r.id for r in active_residents)

    from collections import defaultdict
    care_by_resident: dict[int, list] = defaultdict(list)
    if all_resident_ids:
        all_care = CareRecord.query.options(
            db.joinedload(CareRecord.care_types),
            db.joinedload(CareRecord.care_type),
        ).filter(
            CareRecord.resident_id.in_(all_resident_ids),
            CareRecord.start_time >= hoy_inicio,
            CareRecord.start_time < hoy_fin,
            CareRecord.end_time.isnot(None),
        ).order_by(CareRecord.start_time.desc()).all()
        for c in all_care:
            types = ', '.join(ct.name for ct in c.care_types) if c.care_types else (c.care_type.name if c.care_type else '')
            care_by_resident[c.resident_id].append({
                'time': c.start_time.strftime('%H:%M'),
                'types': types,
                'duration': _format_duration(c.start_time, c.end_time),
            })

    result: list[dict] = []
    for group in worker.groups:
        residents_data: list[dict] = []
        for r in group_residents.get(group.id, []):
            ct = care_by_resident.get(r.id, [])
            residents_data.append({
                'id': r.id,
                'name': r.name,
                'nfc_code': r.nfc_code,
                'room_number': r.room_number or '',
                'photo_url': f'/api/uploads/{r.photo_path}' if r.photo_path else None,
                'has_photo': bool(r.photo_path),
                'has_info': bool(r.relevant_info),
                'has_care_today': len(ct) > 0,
                'care_count_today': len(ct),
                'care_today': ct,
            })
        result.append({
            'id': group.id,
            'name': group.name,
            'color': group.color,
            'residents': residents_data,
        })

    return jsonify({'groups': result}), 200


@app.route('/api/nfc/scan', methods=['POST'])
@jwt_required()
def nfc_scan():
    data = request.json or {}
    nfc_code = str(data.get('nfc_code', '')).strip()
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    mode = data.get('mode')  # opcional: auto-detección si no viene
    care_type_id = data.get('care_type_id')
    dry_run = data.get('dry_run', False)

    if not nfc_code or not worker_id:
        return jsonify({'error': 'Faltan campos requeridos'}), 400

    # Dry run: just look up the resident and return info (for group scan)
    if dry_run and mode == 'care':
        resident = Resident.query.filter_by(nfc_code=nfc_code, active=True).first()
        if not resident and nfc_code.isdigit():
            resident = Resident.query.filter(
                Resident.nfc_code.in_([nfc_code.zfill(4), nfc_code.zfill(5)]),
                Resident.active == True,
            ).first()
        if not resident:
            return jsonify({'error': f'Residente con código "{nfc_code}" no encontrado'}), 404
        return jsonify({
            'action': 'lookup',
            'resident_id': resident.id,
            'resident_name': resident.name,
            'photo_url': f'/api/uploads/{resident.photo_path}' if resident.photo_path else None,
        }), 200

    now = datetime.now()

    # Auto-detección de modo si no viene explícito
    if not mode:
        room = Room.query.filter_by(number=nfc_code).first()
        resident = Resident.query.filter_by(nfc_code=nfc_code, active=True).first()
        # Si no encuentra y el código es numérico, probar con ceros a la izquierda
        if not room and not resident and nfc_code.isdigit():
            padded4 = nfc_code.zfill(4)
            padded5 = nfc_code.zfill(5)
            room = Room.query.filter(Room.number.in_([padded4, padded5])).first()
            resident = Resident.query.filter(Resident.nfc_code.in_([padded4, padded5]), Resident.active == True).first()
        if room and resident:
            return jsonify({
                'action': 'select_mode',
                'room': {'number': room.number, 'description': room.description or ''},
                'resident': {'id': resident.id, 'name': resident.name, 'room_number': resident.room_number or ''},
            }), 200
        if room:
            mode = 'cleaning'
        elif resident:
            mode = 'care'
        else:
            return jsonify({'error': f'Código NFC "{nfc_code}" no reconocido', 'code': 'NFC_NOT_FOUND'}), 404

    if mode == 'cleaning':
        room = Room.query.filter_by(number=nfc_code).first()
        if not room and nfc_code.isdigit():
            room = Room.query.filter(Room.number.in_([nfc_code.zfill(4), nfc_code.zfill(5)])).first()
        if not room:
            return jsonify({'error': f'Habitación "{nfc_code}" no encontrada', 'code': 'ROOM_NOT_FOUND'}), 404

        active_this = CleaningRecord.query.filter_by(
            cleaner_id=worker_id, room_id=room.id, end_time=None
        ).first()
        if active_this:
            active_this.end_time = now
            db.session.commit()
            return jsonify({
                'action': 'ended',
                'type': 'cleaning',
                'record_id': active_this.id,
                'subject': f'Hab. {room.number}',
                'subject_sub': room.description or '',
                'duration': active_this.calculate_duration(),
                'duration_display': _format_duration(active_this.start_time, active_this.end_time),
            }), 200

        # Check single session restriction
        if AppSetting.get('single_session', 'false') == 'true':
            any_active = CleaningRecord.query.filter_by(cleaner_id=worker_id, end_time=None).first() or \
                         CareRecord.query.filter_by(worker_id=worker_id, end_time=None).first()
            if any_active:
                return jsonify({'error': 'Ya tienes una sesión activa. Finalízala antes de iniciar otra.'}), 409

        record = CleaningRecord(cleaner_id=worker_id, room_id=room.id, start_time=now)
        db.session.add(record)
        db.session.commit()
        return jsonify({
            'action': 'started',
            'type': 'cleaning',
            'record_id': record.id,
            'subject': f'Hab. {room.number}',
            'subject_sub': room.description or '',
            'start_time': now.isoformat(),
        }), 200

    if mode == 'care':
        resident = Resident.query.filter_by(nfc_code=nfc_code, active=True).first()
        if not resident and nfc_code.isdigit():
            resident = Resident.query.filter(Resident.nfc_code.in_([nfc_code.zfill(4), nfc_code.zfill(5)]), Resident.active == True).first()
        if not resident:
            return jsonify({'error': f'Residente con código "{nfc_code}" no encontrado', 'code': 'RESIDENT_NOT_FOUND'}), 404

        active_this = CareRecord.query.filter_by(
            worker_id=worker_id, resident_id=resident.id, end_time=None
        ).first()
        if active_this:
            # Don't end directly — ask for care types first
            return jsonify({
                'action': 'select_care_type_end',
                'type': 'care',
                'record_id': active_this.id,
                'resident_id': resident.id,
                'resident_name': resident.name,
                'start_time': active_this.start_time.isoformat(),
                'photo_url': f'/api/uploads/{resident.photo_path}' if resident.photo_path else None,
            }), 200

        # Check single session restriction
        if AppSetting.get('single_session', 'false') == 'true':
            any_active = CleaningRecord.query.filter_by(cleaner_id=worker_id, end_time=None).first() or \
                         CareRecord.query.filter_by(worker_id=worker_id, end_time=None).first()
            if any_active:
                return jsonify({'error': 'Ya tienes una sesión activa. Finalízala antes de iniciar otra.'}), 409

        # Start session immediately without care type
        record = CareRecord(
            worker_id=worker_id,
            resident_id=resident.id,
            start_time=now,
        )
        db.session.add(record)
        db.session.commit()
        return jsonify({
            'action': 'started',
            'type': 'care',
            'record_id': record.id,
            'subject': resident.name,
            'subject_sub': '',
            'start_time': now.isoformat(),
            'photo_url': f'/api/uploads/{resident.photo_path}' if resident.photo_path else None,
        }), 200

    return jsonify({'error': 'Modo no válido. Use "cleaning" o "care"'}), 400


@app.route('/api/nfc/end-session', methods=['POST'])
@jwt_required()
def end_session():
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    record_id = data.get('record_id')
    mode = data.get('mode')

    if mode == 'cleaning':
        record = db.session.get(CleaningRecord, record_id)
        if not record or record.cleaner_id != worker_id or record.end_time:
            return jsonify({'error': 'Registro no válido'}), 400

        # Check if checklist items exist
        checklist_items = ChecklistItem.query.filter_by(active=True).order_by(ChecklistItem.sort_order).all()
        if checklist_items and not data.get('skip_checklist'):
            room = record.room
            return jsonify({
                'action': 'select_checklist',
                'record_id': record.id,
                'subject': f'Hab. {room.number}' if room else 'Habitación',
                'items': [{'id': i.id, 'text': i.text} for i in checklist_items],
            }), 200

        record.end_time = datetime.now()
        db.session.commit()
        room = record.room
        return jsonify({
            'action': 'ended',
            'subject': f'Hab. {room.number}' if room else 'Habitación',
            'subject_sub': room.description or '' if room else '',
            'duration': record.calculate_duration(),
            'duration_display': _format_duration(record.start_time, record.end_time),
        }), 200

    if mode == 'care':
        record = db.session.get(CareRecord, record_id)
        if not record or record.worker_id != worker_id or record.end_time:
            return jsonify({'error': 'Registro no válido'}), 400
        # Ask for care types before ending
        r = record.resident
        return jsonify({
            'action': 'select_care_type_end',
            'type': 'care',
            'record_id': record.id,
            'resident_id': record.resident_id,
            'resident_name': r.name if r else 'Residente',
            'start_time': record.start_time.isoformat(),
            'photo_url': f'/api/uploads/{r.photo_path}' if r and r.photo_path else None,
        }), 200

    return jsonify({'error': 'Modo no válido'}), 400


@app.route('/api/nfc/start-group-care', methods=['POST'])
@jwt_required()
def start_group_care():
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    resident_ids = data.get('resident_ids', [])

    if not worker_id or not resident_ids or len(resident_ids) < 2:
        return jsonify({'error': 'Se necesitan al menos 2 residentes'}), 400

    if len(resident_ids) > 15:
        return jsonify({'error': 'Máximo 15 residentes por grupo'}), 400

    # Check no active care session exists for any of these residents with this worker
    active = CareRecord.query.filter(
        CareRecord.worker_id == worker_id,
        CareRecord.resident_id.in_(resident_ids),
        CareRecord.end_time.is_(None),
    ).first()
    if active:
        r = active.resident
        return jsonify({'error': f'{r.name if r else "Residente"} ya tiene una sesión activa'}), 400

    now = datetime.now()
    records_out = []
    for rid in resident_ids:
        resident = db.session.get(Resident, rid)
        if not resident:
            continue
        rec = CareRecord(worker_id=worker_id, resident_id=rid, start_time=now)
        db.session.add(rec)
        db.session.flush()
        records_out.append({
            'record_id': rec.id,
            'resident_id': rid,
            'resident_name': resident.name,
            'photo_url': f'/api/uploads/{resident.photo_path}' if resident.photo_path else None,
            'start_time': now.isoformat(),
        })

    db.session.commit()
    return jsonify({
        'action': 'group_started',
        'group_key': now.replace(microsecond=0).isoformat(),
        'records': records_out,
    }), 200


@app.route('/api/nfc/finalize-group-care', methods=['POST'])
@jwt_required()
def finalize_group_care():
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    record_mapping = data.get('record_mapping', [])

    if not worker_id or not record_mapping:
        return jsonify({'error': 'Datos incompletos'}), 400

    record_ids = [m['record_id'] for m in record_mapping]
    records = CareRecord.query.filter(
        CareRecord.id.in_(record_ids),
        CareRecord.worker_id == worker_id,
        CareRecord.end_time.is_(None),
    ).all()

    if not records:
        return jsonify({'error': 'Registros no válidos'}), 400

    records_by_id = {r.id: r for r in records}
    now = datetime.now()
    names = []
    all_type_names = set()

    for mapping in record_mapping:
        rec = records_by_id.get(mapping['record_id'])
        if not rec:
            continue
        rec.end_time = now
        for ct_id in mapping.get('care_type_ids', []):
            ct = db.session.get(CareType, ct_id)
            if ct:
                rec.care_types.append(ct)
                all_type_names.add(ct.name)
        if rec.resident:
            names.append(rec.resident.name)

    db.session.commit()

    first = records[0]
    if len(names) > 3:
        subject = ', '.join(names[:3]) + f' (+{len(names) - 3})'
    else:
        subject = ', '.join(names)

    return jsonify({
        'action': 'group_ended',
        'subject': subject,
        'subject_sub': ', '.join(sorted(all_type_names)),
        'duration': first.calculate_duration(),
        'duration_display': _format_duration(first.start_time, first.end_time),
        'count': len(records),
    }), 200


@app.route('/api/nfc/finalize-care', methods=['POST'])
@jwt_required()
def finalize_care():
    data = request.json or {}
    record_id = data.get('record_id')
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    care_type_ids = data.get('care_type_ids', [])
    vital_signs = data.get('vital_signs', [])

    record = db.session.get(CareRecord, record_id)
    if not record or record.worker_id != worker_id or record.end_time:
        return jsonify({'error': 'Registro no válido'}), 400

    record.end_time = datetime.now()
    for ct_id in care_type_ids:
        ct = db.session.get(CareType, ct_id)
        if ct:
            record.care_types.append(ct)

    # Save vital sign readings
    for vs_data in vital_signs:
        vst_id = vs_data.get('vital_sign_type_id')
        val = vs_data.get('value')
        if vst_id is None or val is None or val == '':
            continue
        vst = db.session.get(VitalSignType, vst_id)
        if not vst or not vst.active:
            continue
        try:
            val = float(val)
        except (ValueError, TypeError):
            continue
        if vst.min_value is not None and val < vst.min_value:
            continue
        if vst.max_value is not None and val > vst.max_value:
            continue
        db.session.add(VitalSignReading(care_record_id=record.id, vital_sign_type_id=vst_id, value=val))

    db.session.commit()

    # Check for vital sign alerts
    vital_alerts = []
    for reading in record.vital_sign_readings:
        vst = reading.vital_sign_type
        if (vst.min_value is not None and reading.value < vst.min_value) or \
           (vst.max_value is not None and reading.value > vst.max_value):
            vital_alerts.append({
                'type': vst.name,
                'value': reading.value,
                'unit': vst.unit,
                'alert': 'alta' if (vst.max_value is not None and reading.value > vst.max_value) else 'baja',
            })

    type_names = ', '.join(ct.name for ct in record.care_types)
    resp = {
        'action': 'ended',
        'subject': record.resident.name if record.resident else 'Residente',
        'subject_sub': type_names,
        'duration': record.calculate_duration(),
        'duration_display': _format_duration(record.start_time, record.end_time),
    }
    if vital_alerts:
        resp['vital_alerts'] = vital_alerts
    return jsonify(resp), 200


@app.route('/api/nfc/finalize-cleaning', methods=['POST'])
@jwt_required()
def finalize_cleaning():
    import json as _json
    data = request.json or {}
    record_id = data.get('record_id')
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    checklist = data.get('checklist', [])

    record = db.session.get(CleaningRecord, record_id)
    if not record or record.cleaner_id != worker_id or record.end_time:
        return jsonify({'error': 'Registro no válido'}), 400

    record.end_time = datetime.now()
    record.checklist_json = _json.dumps(checklist, ensure_ascii=False)
    db.session.commit()
    room = record.room
    return jsonify({
        'action': 'ended',
        'subject': f'Hab. {room.number}' if room else 'Habitación',
        'subject_sub': room.description or '' if room else '',
        'duration': record.calculate_duration(),
        'duration_display': _format_duration(record.start_time, record.end_time),
    }), 200


@app.route('/api/nfc/cancel-session', methods=['POST'])
@jwt_required()
def cancel_session():
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    record_id = data.get('record_id')
    record_ids = data.get('record_ids')
    mode = data.get('mode')

    if record_ids and mode == 'care':
        # Group cancel
        for rid in record_ids:
            rec = db.session.get(CareRecord, rid)
            if rec and rec.worker_id == worker_id and not rec.end_time:
                db.session.delete(rec)
    elif record_id and mode:
        if mode == 'cleaning':
            rec = db.session.get(CleaningRecord, record_id)
            if rec and rec.cleaner_id == worker_id and not rec.end_time:
                db.session.delete(rec)
        elif mode == 'care':
            rec = db.session.get(CareRecord, record_id)
            if rec and rec.worker_id == worker_id and not rec.end_time:
                db.session.delete(rec)
    else:
        for c in CleaningRecord.query.filter_by(cleaner_id=worker_id, end_time=None).all():
            db.session.delete(c)
        for c in CareRecord.query.filter_by(worker_id=worker_id, end_time=None).all():
            db.session.delete(c)

    db.session.commit()
    return jsonify({'message': 'Sesión cancelada'}), 200


# ── ADMIN – RESIDENTES ────────────────────────────────────────────────────────

@app.route('/manage-residents')
@admin_required
def manage_residents():
    estado = request.args.get('estado', 'altas')
    query = Resident.query.options(
        joinedload(Resident.group),
        subqueryload(Resident.documents),
    ).order_by(Resident.name)
    if estado == 'altas':
        query = query.filter_by(active=True)
    elif estado == 'bajas':
        query = query.filter_by(active=False)
    residents = query.all()
    groups = ResidentGroup.query.order_by(ResidentGroup.name).all()
    return render_template('manage_residents.html', residents=residents, groups=groups, estado_filtro=estado)


def _save_resident_photo(file_storage, resident_id: int) -> str:
    from PIL import Image
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f'res_{resident_id}_{ts}.jpg'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'residents')
    os.makedirs(folder, exist_ok=True)
    file_storage.seek(0)
    img = Image.open(file_storage)
    img = img.convert('RGB')
    img.thumbnail((800, 800))
    img.save(os.path.join(folder, filename), 'JPEG', quality=85, optimize=True)
    return f'residents/{filename}'


@app.route('/residents/add_edit', methods=['POST'])
@admin_required
def add_edit_resident():
    resident_id = request.form.get('resident_id')
    name = request.form.get('name', '').strip()
    nfc_code = request.form.get('nfc_code', '').strip()
    room_number = request.form.get('room_number', '').strip()
    notes = request.form.get('notes', '').strip()
    relevant_info = request.form.get('relevant_info', '').strip()
    active = bool(request.form.get('active'))
    group_id = request.form.get('group_id', '').strip()
    group_id = int(group_id) if group_id else None

    if not name or not nfc_code:
        flash('El nombre y el código NFC son obligatorios.', 'error')
        return redirect(url_for('manage_residents'))

    try:
        if resident_id:
            r = db.session.get(Resident, int(resident_id))
            if r:
                r.name = name
                r.nfc_code = nfc_code
                r.room_number = room_number or None
                r.notes = notes or None
                r.relevant_info = relevant_info or None
                r.active = active
                r.group_id = group_id
                # Foto
                photo_file = request.files.get('photo')
                if photo_file and photo_file.filename and _allowed_file(photo_file.filename, ALLOWED_IMAGE_EXTENSIONS):
                    # Borrar foto anterior si existe
                    if r.photo_path:
                        old_path = os.path.join(app.config['UPLOAD_FOLDER'], r.photo_path)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    r.photo_path = _save_resident_photo(photo_file, r.id)
                # Permitir quitar foto
                if request.form.get('remove_photo') == '1' and r.photo_path:
                    old_path = os.path.join(app.config['UPLOAD_FOLDER'], r.photo_path)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                    r.photo_path = None
                db.session.commit()
                flash('Residente actualizado correctamente.', 'success')
            else:
                flash('Residente no encontrado.', 'error')
        else:
            r = Resident(
                name=name,
                nfc_code=nfc_code,
                room_number=room_number or None,
                notes=notes or None,
                relevant_info=relevant_info or None,
                active=active,
                group_id=group_id,
            )
            db.session.add(r)
            db.session.flush()  # obtener r.id para el nombre del archivo
            photo_file = request.files.get('photo')
            if photo_file and photo_file.filename:
                r.photo_path = _save_resident_photo(photo_file, r.id)
            db.session.commit()
            flash('Residente añadido correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('El código NFC ya está en uso por otro residente.', 'error')

    return redirect(url_for('manage_residents'))


@app.route('/residents/delete/<int:id>', methods=['POST'])
@admin_required
def delete_resident(id: int):
    r = db.session.get(Resident, id)
    if r is None:
        abort(404)
    try:
        db.session.delete(r)
        db.session.commit()
        flash('Residente eliminado correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque tiene registros de atención asociados.', 'error')
    return redirect(url_for('manage_residents'))


@app.route('/residents/update-group', methods=['POST'])
@admin_required
def update_resident_group():
    data = request.json or {}
    resident_id = data.get('resident_id')
    group_id = data.get('group_id')
    if not resident_id:
        return jsonify({'error': 'resident_id requerido'}), 400
    r = db.session.get(Resident, int(resident_id))
    if not r:
        return jsonify({'error': 'Residente no encontrado'}), 404
    r.group_id = int(group_id) if group_id else None
    db.session.commit()
    return jsonify({'ok': True}), 200


@app.route('/residents/update-active', methods=['POST'])
@admin_required
def update_resident_active():
    data = request.json or {}
    resident_id = data.get('resident_id')
    active = data.get('active')
    if not resident_id or active is None:
        return jsonify({'error': 'resident_id y active requeridos'}), 400
    r = db.session.get(Resident, int(resident_id))
    if not r:
        return jsonify({'error': 'Residente no encontrado'}), 404
    r.active = bool(active)
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── RESIDENT DOCUMENTS ────────────────────────────────────────────────────────

@app.route('/residents/<int:resident_id>/documents', methods=['POST'])
@admin_required
def upload_resident_document(resident_id: int):
    r = db.session.get(Resident, resident_id)
    if not r:
        flash('Residente no encontrado.', 'error')
        return redirect(url_for('manage_residents'))

    doc_file = request.files.get('doc_file')
    if not doc_file or not doc_file.filename:
        flash('Selecciona un archivo.', 'error')
        return redirect(url_for('manage_residents'))

    if not _allowed_file(doc_file.filename, ALLOWED_DOC_EXTENSIONS):
        flash('Tipo de archivo no permitido. Usa PDF, TXT, DOC, DOCX o imágenes.', 'error')
        return redirect(url_for('manage_residents'))

    doc_type = request.form.get('doc_type', '').strip() or 'Otros'
    description = request.form.get('doc_description', '').strip()

    # Save file
    original = doc_file.filename
    safe_name = _secure_filename(original)
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f'{ts}_{safe_name}'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'resident_docs', f'res_{resident_id}')
    os.makedirs(folder, exist_ok=True)
    doc_file.save(os.path.join(folder, filename))
    rel_path = f'resident_docs/res_{resident_id}/{filename}'

    doc = ResidentDocument(
        resident_id=resident_id,
        file_path=rel_path,
        original_filename=original,
        doc_type=doc_type,
        description=description,
    )
    db.session.add(doc)
    db.session.commit()
    flash(f'Documento "{original}" subido correctamente.', 'success')
    return redirect(url_for('manage_residents'))


@app.route('/residents/documents/<int:doc_id>/delete', methods=['POST'])
@admin_required
def delete_resident_document(doc_id: int):
    doc = db.session.get(ResidentDocument, doc_id)
    if not doc:
        flash('Documento no encontrado.', 'error')
        return redirect(url_for('manage_residents'))
    # Delete file
    full_path = os.path.join(app.config['UPLOAD_FOLDER'], doc.file_path)
    if os.path.exists(full_path):
        os.remove(full_path)
    db.session.delete(doc)
    db.session.commit()
    flash('Documento eliminado.', 'success')
    return redirect(url_for('manage_residents'))


@app.route('/groups/<int:id>/assign-residents', methods=['POST'])
@admin_required
def assign_residents_to_group(id: int):
    group = db.session.get(ResidentGroup, id)
    if not group:
        return jsonify({'error': 'Grupo no encontrado'}), 404
    data = request.json or {}
    resident_ids = data.get('resident_ids', [])
    count = 0
    for rid in resident_ids:
        r = db.session.get(Resident, int(rid))
        if r:
            r.group_id = group.id
            count += 1
    db.session.commit()
    return jsonify({'ok': True, 'count': count}), 200


# ── ADMIN – TIPOS DE ATENCIÓN ─────────────────────────────────────────────────

@app.route('/manage-care-types')
@admin_required
def manage_care_types():
    # Show parent types first, then children indented
    parents = CareType.query.filter_by(parent_id=None).order_by(CareType.sort_order, CareType.name).all()
    all_parents = CareType.query.filter_by(parent_id=None).order_by(CareType.name).all()
    # Collect all unique uploaded icons for the icon library
    uploaded_icons = CareType.query.filter(CareType.icon_path.isnot(None)).with_entities(CareType.icon_path).distinct().all()
    uploaded_icons = [row[0] for row in uploaded_icons]
    return render_template('manage_care_types.html', parents=parents, all_parents=all_parents, uploaded_icons=uploaded_icons)


def _save_care_type_icon(file_storage, care_type_id: int) -> str:
    from PIL import Image
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f'ct_{care_type_id}_{ts}.png'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'care_icons')
    os.makedirs(folder, exist_ok=True)
    file_storage.seek(0)
    img = Image.open(file_storage)
    img = img.convert('RGBA')
    img.thumbnail((128, 128))
    img.save(os.path.join(folder, filename), 'PNG', optimize=True)
    return f'care_icons/{filename}'


@app.route('/care-types/add_edit', methods=['POST'])
@admin_required
def add_edit_care_type():
    care_type_id = request.form.get('care_type_id')
    name = request.form.get('name', '').strip()
    icon = request.form.get('icon', '').strip()
    parent_id = request.form.get('parent_id', '').strip()
    sort_order = request.form.get('sort_order', '0').strip()
    selected_icon_path = request.form.get('selected_icon_path', '').strip()

    if not name:
        flash('El nombre es obligatorio.', 'error')
        return redirect(url_for('manage_care_types'))
    try:
        if care_type_id:
            ct = db.session.get(CareType, int(care_type_id))
            if ct:
                ct.name = name
                ct.icon = icon or None
                ct.parent_id = int(parent_id) if parent_id else None
                ct.sort_order = int(sort_order) if sort_order else 0
                # Handle icon: file upload > selected from library > keep existing
                icon_file = request.files.get('icon_file')
                if icon_file and icon_file.filename and _allowed_file(icon_file.filename, ALLOWED_IMAGE_EXTENSIONS):
                    if ct.icon_path:
                        old = os.path.join(app.config['UPLOAD_FOLDER'], ct.icon_path)
                        if os.path.exists(old):
                            os.remove(old)
                    ct.icon_path = _save_care_type_icon(icon_file, ct.id)
                elif selected_icon_path:
                    ct.icon_path = selected_icon_path
                if request.form.get('remove_icon') == '1' and ct.icon_path:
                    old = os.path.join(app.config['UPLOAD_FOLDER'], ct.icon_path)
                    if os.path.exists(old):
                        os.remove(old)
                    ct.icon_path = None
                db.session.commit()
                flash('Tipo actualizado correctamente.', 'success')
            else:
                flash('Tipo no encontrado.', 'error')
        else:
            ct = CareType(
                name=name,
                icon=icon or None,
                parent_id=int(parent_id) if parent_id else None,
                sort_order=int(sort_order) if sort_order else 0,
            )
            db.session.add(ct)
            db.session.flush()
            icon_file = request.files.get('icon_file')
            if icon_file and icon_file.filename:
                ct.icon_path = _save_care_type_icon(icon_file, ct.id)
            elif selected_icon_path:
                ct.icon_path = selected_icon_path
            db.session.commit()
            flash('Tipo añadido correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Error al guardar el tipo.', 'error')
    return redirect(url_for('manage_care_types'))


@app.route('/care-types/delete/<int:id>', methods=['POST'])
@admin_required
def delete_care_type(id: int):
    ct = db.session.get(CareType, id)
    if ct is None:
        abort(404)
    try:
        db.session.delete(ct)
        db.session.commit()
        flash('Tipo eliminado correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque está en uso.', 'error')
    return redirect(url_for('manage_care_types'))


@app.route('/care-types/toggle-active', methods=['POST'])
@admin_required
def toggle_care_type_active():
    data = request.json or {}
    ct = db.session.get(CareType, int(data.get('id', 0)))
    if not ct:
        return jsonify({'error': 'No encontrado'}), 404
    ct.active = data.get('active', True)
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── ADMIN – VITAL SIGN TYPES ────────────────────────────────────────────────

@app.route('/care-types/<int:care_type_id>/vital-fields/add_edit', methods=['POST'])
@admin_required
def add_edit_vital_field(care_type_id: int):
    ct = db.session.get(CareType, care_type_id)
    if not ct:
        flash('Tipo de atención no encontrado.', 'error')
        return redirect(url_for('manage_care_types'))
    vf_id = request.form.get('vf_id')
    name = request.form.get('vf_name', '').strip()
    unit = request.form.get('vf_unit', '').strip()
    min_val = request.form.get('vf_min', '').strip()
    max_val = request.form.get('vf_max', '').strip()
    input_type = request.form.get('vf_input_type', 'number').strip()
    sort_order = request.form.get('vf_sort_order', '0').strip()
    if not name or not unit:
        flash('Nombre y unidad son obligatorios.', 'error')
        return redirect(url_for('manage_care_types'))
    if vf_id:
        vf = db.session.get(VitalSignType, int(vf_id))
        if vf:
            vf.name = name
            vf.unit = unit
            vf.min_value = float(min_val) if min_val else None
            vf.max_value = float(max_val) if max_val else None
            vf.input_type = input_type
            vf.sort_order = int(sort_order) if sort_order else 0
    else:
        vf = VitalSignType(
            care_type_id=care_type_id, name=name, unit=unit,
            min_value=float(min_val) if min_val else None,
            max_value=float(max_val) if max_val else None,
            input_type=input_type,
            sort_order=int(sort_order) if sort_order else 0,
        )
        db.session.add(vf)
    db.session.commit()
    flash(f'Campo vital "{name}" guardado.', 'success')
    return redirect(url_for('manage_care_types'))


@app.route('/care-types/vital-fields/delete/<int:vf_id>', methods=['POST'])
@admin_required
def delete_vital_field(vf_id: int):
    vf = db.session.get(VitalSignType, vf_id)
    if not vf:
        flash('Campo no encontrado.', 'error')
        return redirect(url_for('manage_care_types'))
    if vf.readings:
        vf.active = False
        flash(f'Campo "{vf.name}" desactivado (tiene lecturas asociadas).', 'warning')
    else:
        db.session.delete(vf)
        flash(f'Campo "{vf.name}" eliminado.', 'success')
    db.session.commit()
    return redirect(url_for('manage_care_types'))


# ── ADMIN – CHECKLIST DE LIMPIEZA ────────────────────────────────────────────

@app.route('/manage-checklist')
@admin_required
def manage_checklist():
    items = ChecklistItem.query.order_by(ChecklistItem.sort_order, ChecklistItem.id).all()
    return render_template('manage_checklist.html', items=items)


@app.route('/checklist/add_edit', methods=['POST'])
@admin_required
def add_edit_checklist_item():
    item_id = request.form.get('item_id')
    text = request.form.get('text', '').strip()
    sort_order = request.form.get('sort_order', '0').strip()

    if not text:
        flash('El texto es obligatorio.', 'error')
        return redirect(url_for('manage_checklist'))

    if item_id:
        item = db.session.get(ChecklistItem, int(item_id))
        if item:
            item.text = text
            item.sort_order = int(sort_order) if sort_order else 0
            db.session.commit()
            flash('Item actualizado.', 'success')
    else:
        item = ChecklistItem(text=text, sort_order=int(sort_order) if sort_order else 0)
        db.session.add(item)
        db.session.commit()
        flash('Item añadido.', 'success')
    return redirect(url_for('manage_checklist'))


@app.route('/checklist/delete/<int:id>', methods=['POST'])
@admin_required
def delete_checklist_item(id: int):
    item = db.session.get(ChecklistItem, id)
    if item:
        db.session.delete(item)
        db.session.commit()
        flash('Item eliminado.', 'success')
    return redirect(url_for('manage_checklist'))


@app.route('/checklist/toggle-active', methods=['POST'])
@admin_required
def toggle_checklist_active():
    data = request.json or {}
    item = db.session.get(ChecklistItem, int(data.get('id', 0)))
    if not item:
        return jsonify({'error': 'No encontrado'}), 404
    item.active = data.get('active', True)
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── ADMIN – GRUPOS DE RESIDENTES ─────────────────────────────────────────────

@app.route('/manage-groups')
@admin_required
def manage_groups():
    groups = ResidentGroup.query.order_by(ResidentGroup.name).all()
    return render_template('manage_groups.html', groups=groups)


@app.route('/groups/<int:id>')
@admin_required
def group_detail(id: int):
    group = db.session.get(ResidentGroup, id)
    if group is None:
        abort(404)
    residents = Resident.query.filter_by(group_id=group.id).order_by(Resident.name).all()
    available = Resident.query.filter(
        Resident.active == True,
        (Resident.group_id == None) | (Resident.group_id != group.id),
    ).order_by(Resident.name).all()
    return render_template('group_detail.html', group=group, residents=residents, available=available)


@app.route('/groups/add_edit', methods=['POST'])
@admin_required
def add_edit_group():
    group_id = request.form.get('group_id')
    name = request.form.get('name', '').strip()
    color = request.form.get('color', '#000000').strip()

    if not name:
        flash('El nombre es obligatorio.', 'error')
        return redirect(url_for('manage_groups'))

    try:
        if group_id:
            g = db.session.get(ResidentGroup, int(group_id))
            if g:
                g.name = name
                g.color = color
                db.session.commit()
                flash('Grupo actualizado correctamente.', 'success')
            else:
                flash('Grupo no encontrado.', 'error')
        else:
            db.session.add(ResidentGroup(name=name, color=color))
            db.session.commit()
            flash('Grupo añadido correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Ya existe un grupo con ese nombre.', 'error')

    return redirect(url_for('manage_groups'))


@app.route('/groups/delete/<int:id>', methods=['POST'])
@admin_required
def delete_group(id: int):
    g = db.session.get(ResidentGroup, id)
    if g is None:
        abort(404)
    try:
        db.session.delete(g)
        db.session.commit()
        flash('Grupo eliminado correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque tiene residentes o trabajadores asignados.', 'error')
    return redirect(url_for('manage_groups'))


# ── ADMIN – FICHAJES POR TRABAJADOR ──────────────────────────────────────────

def _build_fichajes(worker_id: int, year: int, mon: int) -> list[dict]:
    month_start = datetime(year, mon, 1)
    month_end = datetime(year + 1, 1, 1) if mon == 12 else datetime(year, mon + 1, 1)

    records: list[dict] = []

    cleanings = CleaningRecord.query.options(
        joinedload(CleaningRecord.room),
    ).filter(
        CleaningRecord.cleaner_id == worker_id,
        CleaningRecord.start_time >= month_start,
        CleaningRecord.start_time < month_end,
    ).all()

    for c in cleanings:
        label = f'Limpieza - Hab. {c.room.number}' if c.room else 'Limpieza'
        detail = c.room.description if c.room else ''
        if c.start_time:
            records.append({
                'datetime': c.start_time,
                'date': c.start_time.strftime('%d/%m/%Y'),
                'time': c.start_time.strftime('%H:%M'),
                'type': 'Inicio',
                'category': 'Limpieza',
                'label': label,
                'detail': detail,
            })
        if c.end_time:
            records.append({
                'datetime': c.end_time,
                'date': c.end_time.strftime('%d/%m/%Y'),
                'time': c.end_time.strftime('%H:%M'),
                'type': 'Fin',
                'category': 'Limpieza',
                'label': label,
                'detail': detail,
            })

    cares = CareRecord.query.options(
        joinedload(CareRecord.resident),
        joinedload(CareRecord.care_type),
    ).filter(
        CareRecord.worker_id == worker_id,
        CareRecord.start_time >= month_start,
        CareRecord.start_time < month_end,
    ).all()

    for c in cares:
        label = f'Atención - {c.resident.name}' if c.resident else 'Atención'
        detail = c.care_type.name if c.care_type else ''
        if c.start_time:
            records.append({
                'datetime': c.start_time,
                'date': c.start_time.strftime('%d/%m/%Y'),
                'time': c.start_time.strftime('%H:%M'),
                'type': 'Inicio',
                'category': 'Atención',
                'label': label,
                'detail': detail,
            })
        if c.end_time:
            records.append({
                'datetime': c.end_time,
                'date': c.end_time.strftime('%d/%m/%Y'),
                'time': c.end_time.strftime('%H:%M'),
                'type': 'Fin',
                'category': 'Atención',
                'label': label,
                'detail': detail,
            })

    records.sort(key=lambda r: r['datetime'])
    return records


@app.route('/fichajes')
@admin_required
def fichajes_trabajador():
    worker_id = request.args.get('worker_id', '', type=str)
    month = request.args.get('month', '')

    records: list[dict] = []
    selected_worker = None

    if worker_id and month:
        try:
            year, mon = int(month[:4]), int(month[5:7])
        except (ValueError, IndexError):
            flash('Formato de mes no válido.', 'error')
            return redirect(url_for('fichajes_trabajador'))

        selected_worker = db.session.get(Cleaner, int(worker_id))
        records = _build_fichajes(int(worker_id), year, mon)

    filters = {'worker_id': worker_id, 'month': month}

    return render_template(
        'fichajes.html',
        records=records,
        workers=Cleaner.query.order_by(Cleaner.name).all(),
        filters=filters,
        selected_worker=selected_worker,
    )


@app.route('/exportar_fichajes')
@admin_required
def exportar_fichajes():
    worker_id = request.args.get('worker_id', '', type=str)
    month = request.args.get('month', '')

    if not worker_id or not month:
        flash('Selecciona un trabajador y un mes.', 'error')
        return redirect(url_for('fichajes_trabajador'))

    try:
        year, mon = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        flash('Formato de mes no válido.', 'error')
        return redirect(url_for('fichajes_trabajador'))

    worker = db.session.get(Cleaner, int(worker_id))
    worker_name = worker.name if worker else 'Desconocido'

    records = _build_fichajes(int(worker_id), year, mon)

    data = [{
        'Fecha': r['date'],
        'Hora': r['time'],
        'Tipo': r['type'],
        'Categoría': r['category'],
        'Actividad': r['label'],
        'Detalle': r['detail'],
    } for r in records]

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='Fichajes', index=False)
    output.seek(0)

    filename = f'fichajes_{worker_name}_{year}-{mon:02d}.xlsx'

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name=filename,
    )


# ── ADMIN – REGISTROS DE ATENCIÓN ─────────────────────────────────────────────

@app.route('/registros-atencion')
@admin_required
def registros_atencion():
    worker_id = request.args.get('worker_id', '')
    resident_id = request.args.get('resident_id', '')
    care_type_id = request.args.get('care_type_id', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    query = CareRecord.query.options(
        joinedload(CareRecord.resident),
        joinedload(CareRecord.worker),
        joinedload(CareRecord.care_type),
        joinedload(CareRecord.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
    )

    if worker_id:
        query = query.filter(CareRecord.worker_id == worker_id)
    if resident_id:
        query = query.filter(CareRecord.resident_id == resident_id)
    if care_type_id:
        query = query.filter(CareRecord.care_type_id == care_type_id)
    if start_date:
        query = query.filter(CareRecord.start_time >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
        query = query.filter(CareRecord.start_time < end_dt)

    query = query.order_by(CareRecord.start_time.desc())
    page = request.args.get('page', 1, type=int)
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    for record in pagination.items:
        record.duration = _format_duration(record.start_time, record.end_time)

    filters = {
        'worker_id': worker_id,
        'resident_id': resident_id,
        'care_type_id': care_type_id,
        'start_date': start_date,
        'end_date': end_date,
    }

    return render_template(
        'registros_atencion.html',
        records=pagination.items,
        pagination=pagination,
        workers=Cleaner.query.order_by(Cleaner.name).all(),
        residents=Resident.query.order_by(Resident.name).all(),
        care_types=CareType.query.order_by(CareType.name).all(),
        filters=filters,
    )


@app.route('/admin/resident/<int:resident_id>')
@admin_required
def resident_detail(resident_id: int):
    resident = db.session.get(Resident, resident_id)
    if not resident:
        flash('Residente no encontrado.', 'error')
        return redirect(url_for('manage_residents'))

    query = CareRecord.query.options(
        joinedload(CareRecord.worker),
        joinedload(CareRecord.care_type),
        subqueryload(CareRecord.care_types),
        subqueryload(CareRecord.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
    ).filter(CareRecord.resident_id == resident_id).order_by(CareRecord.start_time.desc())

    page = request.args.get('page', 1, type=int)
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    for record in pagination.items:
        record.duration = _format_duration(record.start_time, record.end_time)

    # Vital signs chart data: all readings for this resident, grouped by type
    all_readings = db.session.query(VitalSignReading).join(CareRecord).filter(
        CareRecord.resident_id == resident_id
    ).join(VitalSignReading.vital_sign_type).order_by(CareRecord.start_time.asc()).all()

    vital_charts = {}
    for r in all_readings:
        vst = r.vital_sign_type
        if not vst or not r.care_record or not r.care_record.start_time or r.value is None:
            continue
        key = vst.id
        if key not in vital_charts:
            vital_charts[key] = {
                'name': vst.name,
                'unit': vst.unit,
                'min_value': float(vst.min_value) if vst.min_value is not None else None,
                'max_value': float(vst.max_value) if vst.max_value is not None else None,
                'labels': [],
                'data': [],
            }
        vital_charts[key]['labels'].append(r.care_record.start_time.strftime('%d/%m/%Y %H:%M'))
        vital_charts[key]['data'].append(float(r.value))

    # Care activity heatmap: count care records per day (last 6 months)
    six_months_ago = datetime.now() - timedelta(days=180)
    care_dates = db.session.query(
        db.func.date(CareRecord.start_time).label('day'),
        db.func.count().label('count'),
    ).filter(
        CareRecord.resident_id == resident_id,
        CareRecord.start_time >= six_months_ago,
    ).group_by(db.func.date(CareRecord.start_time)).all()

    heatmap_data = {str(row.day): row.count for row in care_dates}

    return render_template(
        'resident_detail.html',
        resident=resident,
        records=pagination.items,
        pagination=pagination,
        vital_charts=list(vital_charts.values()),
        heatmap_data=heatmap_data,
    )


@app.route('/admin/resident/<int:resident_id>/export-excel')
@admin_required
def export_resident_care_excel(resident_id: int):
    resident = db.session.get(Resident, resident_id)
    if not resident:
        abort(404)
    records = CareRecord.query.options(
        joinedload(CareRecord.worker),
        joinedload(CareRecord.care_types),
        joinedload(CareRecord.care_type),
        joinedload(CareRecord.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
    ).filter(CareRecord.resident_id == resident_id).order_by(CareRecord.start_time.desc()).all()

    data = []
    for r in records:
        types = ', '.join(ct.name for ct in r.care_types) if r.care_types else (r.care_type.name if r.care_type else '')
        vitals = '; '.join(f'{v.vital_sign_type.name}: {v.value} {v.vital_sign_type.unit}' for v in r.vital_sign_readings) if r.vital_sign_readings else ''
        data.append({
            'Tipo de atención': types,
            'Trabajador': r.worker.name if r.worker else '',
            'Fecha': r.start_time.strftime('%d/%m/%Y') if r.start_time else '',
            'Hora': r.start_time.strftime('%H:%M') if r.start_time else '',
            'Duración': _format_duration(r.start_time, r.end_time),
            'Constantes vitales': vitals,
        })

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='Atenciones', index=False)
    output.seek(0)

    safe_name = resident.name.replace(' ', '_')
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name=f'atenciones_{safe_name}.xlsx',
    )


@app.route('/admin/care-record/<int:record_id>/delete', methods=['POST'])
@admin_required
def delete_care_record(record_id: int):
    if not current_user.is_admin:
        abort(403)
    record = db.session.get(CareRecord, record_id)
    if not record:
        flash('Registro no encontrado.', 'error')
        return redirect(url_for('registros_atencion'))
    VitalSignReading.query.filter_by(care_record_id=record.id).delete()
    record.care_types.clear()
    db.session.delete(record)
    db.session.commit()
    flash('Registro de atención eliminado.', 'success')
    return redirect(url_for('registros_atencion'))


# ══════════════════════════════════════════════════════════════════════════════
#  UPLOADS — Servir fitxers
# ══════════════════════════════════════════════════════════════════════════════

def _save_base64_photo(b64_data: str, subfolder: str, cleaner_id: int) -> str:
    """Decodifica base64 (data URI o raw), re-processa com a JPEG via Pillow i retorna el path relatiu."""
    from PIL import Image
    from io import BytesIO
    if ',' in b64_data:
        b64_data = b64_data.split(',', 1)[1]
    img_bytes = base64.b64decode(b64_data)
    img = Image.open(BytesIO(img_bytes))
    img = img.convert('RGB')
    img.thumbnail((800, 800))
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f'{cleaner_id}_{ts}.jpg'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], subfolder)
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, filename)
    img.save(filepath, 'JPEG', quality=85, optimize=True)
    return f'{subfolder}/{filename}'


@app.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename: str):
    if '..' in filename:
        abort(400)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/api/uploads/<path:filename>')
@jwt_required()
def api_serve_upload(filename: str):
    if '..' in filename:
        abort(400)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ══════════════════════════════════════════════════════════════════════════════
#  IDENTITAT — Selfie d'alta
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/worker/identity-status')
@jwt_required()
def worker_identity_status():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    cleaner = db.session.get(Cleaner, worker_id)
    if not cleaner:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
    return jsonify({'verified': cleaner.identity_verified})


@app.route('/api/worker/enroll-selfie', methods=['POST'])
@jwt_required()
def enroll_selfie():
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    photo = data.get('photo')
    if not worker_id or not photo:
        return jsonify({'error': 'worker_id y photo requeridos'}), 400
    cleaner = db.session.get(Cleaner, int(worker_id))
    if not cleaner:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
    try:
        path = _save_base64_photo(photo, 'selfies', cleaner.id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    selfie = WorkerSelfie(
        cleaner_id=cleaner.id, photo_path=path,
        is_reference=True, purpose='enrollment',
    )
    db.session.add(selfie)
    cleaner.identity_verified = True
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/worker/verify-selfie', methods=['POST'])
@jwt_required()
def verify_selfie():
    """Guarda una selfie de verificació (per signatura o formació)."""
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    photo = data.get('photo')
    purpose = data.get('purpose', 'verification')
    if not worker_id or not photo:
        return jsonify({'error': 'worker_id y photo requeridos'}), 400
    cleaner = db.session.get(Cleaner, int(worker_id))
    if not cleaner:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
    try:
        subfolder = 'signing_selfies' if purpose == 'signing' else 'selfies'
        path = _save_base64_photo(photo, subfolder, cleaner.id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    selfie = WorkerSelfie(
        cleaner_id=cleaner.id, photo_path=path,
        is_reference=False, purpose=purpose,
    )
    db.session.add(selfie)
    db.session.commit()
    return jsonify({'ok': True, 'path': path})


# ══════════════════════════════════════════════════════════════════════════════
#  DOCUMENTS LEGALS — Admin
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/admin/documents')
@admin_required
def admin_documents():
    docs = LegalDocument.query.order_by(LegalDocument.created_at.desc()).all()
    workers = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    return render_template('admin_documents.html', documents=docs, workers=workers)


@app.route('/admin/documents/create', methods=['POST'])
@admin_required
def create_document():
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    doc_type = request.form.get('doc_type', '').strip()
    if not title or not content:
        flash('El título y el contenido son obligatorios.', 'error')
        return redirect(url_for('admin_documents'))
    doc = LegalDocument(
        title=title, content=content, doc_type=doc_type or None,
        created_by=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    flash('Documento creado correctamente.', 'success')
    return redirect(url_for('admin_documents'))


@app.route('/admin/documents/<int:doc_id>/edit', methods=['POST'])
@admin_required
def edit_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    if doc.signatures:
        flash('No se puede editar un documento que ya tiene firmas.', 'error')
        return redirect(url_for('admin_documents'))
    doc.title = request.form.get('title', '').strip() or doc.title
    doc.content = request.form.get('content', '').strip() or doc.content
    doc.doc_type = request.form.get('doc_type', '').strip() or doc.doc_type
    db.session.commit()
    flash('Documento actualizado.', 'success')
    return redirect(url_for('admin_documents'))


@app.route('/admin/documents/<int:doc_id>/delete', methods=['POST'])
@admin_required
def delete_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    if doc.signatures:
        flash('No se puede eliminar un documento que ya tiene firmas.', 'error')
        return redirect(url_for('admin_documents'))
    db.session.delete(doc)
    db.session.commit()
    flash('Documento eliminado.', 'success')
    return redirect(url_for('admin_documents'))


@app.route('/admin/documents/<int:doc_id>/toggle', methods=['POST'])
@admin_required
def toggle_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    doc.active = not doc.active
    db.session.commit()
    flash(f'Documento {"activado" if doc.active else "desactivado"}.', 'success')
    return redirect(url_for('admin_documents'))


@app.route('/admin/documents/<int:doc_id>/signatures')
@admin_required
def document_signatures(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    sigs = DocumentSignature.query.filter_by(document_id=doc_id)\
        .options(joinedload(DocumentSignature.cleaner))\
        .order_by(DocumentSignature.signed_at.desc()).all()
    workers = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    signed_ids = {s.cleaner_id for s in sigs}
    unsigned = [w for w in workers if w.id not in signed_ids]
    return render_template('admin_document_signatures.html',
                           document=doc, signatures=sigs, unsigned=unsigned)


# ══════════════════════════════════════════════════════════════════════════════
#  DOCUMENTS LEGALS — Worker API
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/worker/pending-documents')
@jwt_required()
def pending_documents():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    signed = db.session.query(DocumentSignature.document_id)\
        .filter_by(cleaner_id=worker_id).subquery()
    docs = LegalDocument.query.filter_by(active=True)\
        .filter(~LegalDocument.id.in_(signed))\
        .order_by(LegalDocument.created_at).all()
    return jsonify([{
        'id': d.id, 'title': d.title, 'doc_type': d.doc_type or '',
    } for d in docs])


@app.route('/api/worker/document/<int:doc_id>')
@jwt_required()
def get_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        return jsonify({'error': 'Documento no encontrado'}), 404
    return jsonify({
        'id': doc.id, 'title': doc.title, 'content': doc.content,
        'doc_type': doc.doc_type or '',
    })


@app.route('/api/worker/document/<int:doc_id>/sign', methods=['POST'])
@jwt_required()
def sign_document(doc_id: int):
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    photo = data.get('photo')
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        return jsonify({'error': 'Documento no encontrado'}), 404
    existing = DocumentSignature.query.filter_by(
        document_id=doc_id, cleaner_id=worker_id).first()
    if existing:
        return jsonify({'error': 'Ya has firmado este documento'}), 400
    selfie_path = None
    if photo:
        try:
            selfie_path = _save_base64_photo(photo, 'signing_selfies', int(worker_id))
        except ValueError:
            pass
    content_hash = hashlib.sha256(doc.content.encode()).hexdigest()
    sig = DocumentSignature(
        document_id=doc_id, cleaner_id=int(worker_id),
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent', '')[:500],
        selfie_path=selfie_path, content_hash=content_hash,
    )
    db.session.add(sig)
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  PÍNDOLES FORMATIVES — Admin
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/admin/help')
@admin_required
def admin_help():
    return render_template('admin_help.html')


@app.route('/admin/analytics')
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


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    if request.method == 'POST':
        AppSetting.set('allow_group_care', 'true' if request.form.get('allow_group_care') else 'false')
        AppSetting.set('nfc_only', 'true' if request.form.get('nfc_only') else 'false')
        AppSetting.set('single_session', 'true' if request.form.get('single_session') else 'false')
        flash('Configuración guardada.', 'success')
        return redirect(url_for('admin_settings'))
    return render_template('admin_settings.html',
        allow_group_care=AppSetting.get('allow_group_care', 'true') == 'true',
        nfc_only=AppSetting.get('nfc_only', 'true') == 'true',
        single_session=AppSetting.get('single_session', 'false') == 'true',
    )


@app.route('/api/config')
@jwt_required()
def api_config():
    return jsonify({
        'allow_group_care': AppSetting.get('allow_group_care', 'true') == 'true',
        'nfc_only': AppSetting.get('nfc_only', 'true') == 'true',
        'single_session': AppSetting.get('single_session', 'false') == 'true',
    }), 200


@app.route('/admin/training')
@admin_required
def admin_training():
    pills = TrainingPill.query.order_by(TrainingPill.created_at.desc()).all()
    total_workers = Cleaner.query.filter_by(active=True, is_admin=False).count()
    pills_json = {p.id: {
        'title': p.title, 'description': p.description or '',
        'video_url': p.video_url or '',
        'video_duration_seconds': p.video_duration_seconds or '',
        'pass_threshold': p.pass_threshold,
        'questions': [{
            'question_text': q.question_text,
            'option_a': q.option_a, 'option_b': q.option_b,
            'option_c': q.option_c, 'option_d': q.option_d,
            'correct_option': q.correct_option,
        } for q in p.questions],
    } for p in pills}
    return render_template('admin_training.html', pills=pills,
                           total_workers=total_workers, pills_json=pills_json)


@app.route('/admin/training/create', methods=['POST'])
@admin_required
def create_training():
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    video_url = request.form.get('video_url', '').strip()
    duration = request.form.get('video_duration_seconds', type=int) or None
    threshold = request.form.get('pass_threshold', 80, type=int)
    if not title:
        flash('El título es obligatorio.', 'error')
        return redirect(url_for('admin_training'))
    pill = TrainingPill(
        title=title, description=description or None,
        video_url=video_url or None, video_duration_seconds=duration,
        pass_threshold=threshold, created_by=current_user.id,
    )
    db.session.add(pill)
    db.session.flush()
    # Preguntes
    idx = 0
    while request.form.get(f'q_{idx}_text'):
        q = TrainingQuestion(
            pill_id=pill.id,
            question_text=request.form[f'q_{idx}_text'].strip(),
            option_a=request.form.get(f'q_{idx}_a', '').strip(),
            option_b=request.form.get(f'q_{idx}_b', '').strip(),
            option_c=request.form.get(f'q_{idx}_c', '').strip(),
            option_d=request.form.get(f'q_{idx}_d', '').strip(),
            correct_option=request.form.get(f'q_{idx}_correct', 'a').strip(),
            sort_order=idx,
        )
        db.session.add(q)
        idx += 1
    db.session.commit()
    flash('Píldora formativa creada.', 'success')
    return redirect(url_for('admin_training'))


@app.route('/admin/training/<int:pill_id>/edit', methods=['POST'])
@admin_required
def edit_training(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    pill.title = request.form.get('title', '').strip() or pill.title
    pill.description = request.form.get('description', '').strip() or None
    pill.video_url = request.form.get('video_url', '').strip() or None
    pill.video_duration_seconds = request.form.get('video_duration_seconds', type=int) or None
    pill.pass_threshold = request.form.get('pass_threshold', 80, type=int)
    # Reemplaçar preguntes
    TrainingQuestion.query.filter_by(pill_id=pill.id).delete()
    idx = 0
    while request.form.get(f'q_{idx}_text'):
        q = TrainingQuestion(
            pill_id=pill.id,
            question_text=request.form[f'q_{idx}_text'].strip(),
            option_a=request.form.get(f'q_{idx}_a', '').strip(),
            option_b=request.form.get(f'q_{idx}_b', '').strip(),
            option_c=request.form.get(f'q_{idx}_c', '').strip(),
            option_d=request.form.get(f'q_{idx}_d', '').strip(),
            correct_option=request.form.get(f'q_{idx}_correct', 'a').strip(),
            sort_order=idx,
        )
        db.session.add(q)
        idx += 1
    db.session.commit()
    flash('Píldora formativa actualizada.', 'success')
    return redirect(url_for('admin_training'))


@app.route('/admin/training/<int:pill_id>/delete', methods=['POST'])
@admin_required
def delete_training(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    TrainingCompletion.query.filter_by(pill_id=pill.id).delete()
    TrainingQuestion.query.filter_by(pill_id=pill.id).delete()
    db.session.delete(pill)
    db.session.commit()
    flash('Píldora formativa eliminada.', 'success')
    return redirect(url_for('admin_training'))


@app.route('/admin/training/<int:pill_id>/toggle', methods=['POST'])
@admin_required
def toggle_training(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    pill.active = not pill.active
    db.session.commit()
    flash(f'Píldora {"activada" if pill.active else "desactivada"}.', 'success')
    return redirect(url_for('admin_training'))


@app.route('/admin/training/<int:pill_id>/results')
@admin_required
def training_results(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    completions = TrainingCompletion.query.filter_by(pill_id=pill_id)\
        .options(joinedload(TrainingCompletion.cleaner))\
        .order_by(TrainingCompletion.completed_at.desc()).all()
    workers = Cleaner.query.filter_by(active=True, is_admin=False).order_by(Cleaner.name).all()
    completed_ids = {c.cleaner_id for c in completions if c.passed}
    pending = [w for w in workers if w.id not in completed_ids]
    return render_template('admin_training_results.html',
                           pill=pill, completions=completions, pending=pending)


# ══════════════════════════════════════════════════════════════════════════════
#  PÍNDOLES FORMATIVES — Worker API
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/worker/pending-training')
@jwt_required()
def pending_training():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    passed = db.session.query(TrainingCompletion.pill_id)\
        .filter_by(cleaner_id=worker_id, passed=True).subquery()
    pills = TrainingPill.query.filter_by(active=True)\
        .filter(~TrainingPill.id.in_(passed))\
        .order_by(TrainingPill.created_at).all()
    return jsonify([{
        'id': p.id, 'title': p.title,
        'description': p.description or '',
        'question_count': len(p.questions),
    } for p in pills])


@app.route('/api/worker/training/<int:pill_id>')
@jwt_required()
def get_training(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        return jsonify({'error': 'Píldora no encontrada'}), 404
    return jsonify({
        'id': pill.id, 'title': pill.title,
        'description': pill.description or '',
        'video_url': pill.video_url or '',
        'video_duration_seconds': pill.video_duration_seconds or 60,
        'pass_threshold': pill.pass_threshold,
        'question_count': len(pill.questions),
    })


@app.route('/api/worker/training/<int:pill_id>/start', methods=['POST'])
@jwt_required()
def start_training(pill_id: int):
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    photo = data.get('photo')
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        return jsonify({'error': 'Píldora no encontrada'}), 404
    # Guardar selfie si la hi ha
    if photo:
        try:
            _save_base64_photo(photo, 'selfies', int(worker_id))
        except ValueError:
            pass
    # Crear o reaprofitar completions anteriors no aprovades
    completion = TrainingCompletion.query.filter_by(
        pill_id=pill_id, cleaner_id=int(worker_id), passed=False,
    ).first()
    if not completion:
        completion = TrainingCompletion(
            pill_id=pill_id, cleaner_id=int(worker_id),
        )
        db.session.add(completion)
    else:
        completion.started_at = datetime.now()
        completion.video_watched = False
        completion.completed_at = None
        completion.score = None
        completion.passed = None
    db.session.commit()
    return jsonify({'ok': True, 'completion_id': completion.id})


@app.route('/api/worker/training/<int:pill_id>/video-complete', methods=['POST'])
@jwt_required()
def training_video_complete(pill_id: int):
    data = request.json or {}
    worker_id = data.get('worker_id')
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    completion = TrainingCompletion.query.filter_by(
        pill_id=pill_id, cleaner_id=int(worker_id),
    ).order_by(TrainingCompletion.started_at.desc()).first()
    if not completion:
        return jsonify({'error': 'No hay sesión activa'}), 400
    # Anti-trampa: mínim 50% de la durada del vídeo
    pill = db.session.get(TrainingPill, pill_id)
    min_secs = (pill.video_duration_seconds or 60) * 0.5
    elapsed = (datetime.now() - completion.started_at).total_seconds()
    if elapsed < min_secs:
        return jsonify({'error': 'Debes ver el vídeo completo', 'wait': int(min_secs - elapsed)}), 400
    completion.video_watched = True
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/worker/training/<int:pill_id>/questions')
@jwt_required()
def training_questions(pill_id: int):
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        return jsonify({'error': 'Píldora no encontrada'}), 404
    completion = TrainingCompletion.query.filter_by(
        pill_id=pill_id, cleaner_id=worker_id,
    ).order_by(TrainingCompletion.started_at.desc()).first()
    if not completion or not completion.video_watched:
        return jsonify({'error': 'Primero debes ver el vídeo'}), 400
    # Barrejar preguntes i opcions
    questions = list(pill.questions)
    random.shuffle(questions)
    shuffle_map = {}  # {shuffled_q_index: {question_id, option_map: {a: orig, b: orig...}}}
    result = []
    for i, q in enumerate(questions):
        options = [('a', q.option_a), ('b', q.option_b), ('c', q.option_c), ('d', q.option_d)]
        random.shuffle(options)
        option_map = {}
        shuffled_options = {}
        for new_key, (orig_key, text) in zip(['a', 'b', 'c', 'd'], options):
            option_map[new_key] = orig_key
            shuffled_options[new_key] = text
        shuffle_map[str(i)] = {'question_id': q.id, 'option_map': option_map}
        result.append({
            'index': i,
            'question': q.question_text,
            'options': shuffled_options,
        })
    completion.shuffle_map = json.dumps(shuffle_map)
    db.session.commit()
    return jsonify(result)


@app.route('/api/worker/training/<int:pill_id>/submit', methods=['POST'])
@jwt_required()
def submit_training(pill_id: int):
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    answers = data.get('answers', {})  # {"0": "a", "1": "c", ...}
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    completion = TrainingCompletion.query.filter_by(
        pill_id=pill_id, cleaner_id=int(worker_id),
    ).order_by(TrainingCompletion.started_at.desc()).first()
    if not completion or not completion.video_watched:
        return jsonify({'error': 'Sesión no válida'}), 400
    if not completion.shuffle_map:
        return jsonify({'error': 'Preguntas no cargadas'}), 400
    smap = json.loads(completion.shuffle_map)
    correct = 0
    total = len(smap)
    for q_idx, mapping in smap.items():
        q = db.session.get(TrainingQuestion, mapping['question_id'])
        user_answer = answers.get(q_idx)
        if user_answer and mapping['option_map'].get(user_answer) == q.correct_option:
            correct += 1
    score = int(correct / total * 100) if total else 0
    pill = db.session.get(TrainingPill, pill_id)
    completion.score = score
    completion.passed = score >= pill.pass_threshold
    completion.completed_at = datetime.now()
    completion.answers_json = json.dumps(answers)
    completion.time_spent_seconds = int(
        (completion.completed_at - completion.started_at).total_seconds())
    db.session.commit()
    return jsonify({
        'score': score,
        'passed': completion.passed,
        'correct': correct,
        'total': total,
        'threshold': pill.pass_threshold,
    })


@app.cli.command('auto-assign-roles')
def auto_assign_roles():
    """Assign worker roles (limpieza/atenciones/mixto) based on historical data."""
    workers = Cleaner.query.filter_by(active=True).all()
    stats = {'limpieza': 0, 'atenciones': 0, 'mixto': 0, 'sin_actividad': 0}
    for w in workers:
        cleanings = CleaningRecord.query.filter_by(cleaner_id=w.id).count()
        cares = CareRecord.query.filter_by(worker_id=w.id).count()
        if cleanings > 0 and cares > 0:
            w.role = 'mixto'
            stats['mixto'] += 1
        elif cleanings > 0:
            w.role = 'limpieza'
            stats['limpieza'] += 1
        elif cares > 0:
            w.role = 'atenciones'
            stats['atenciones'] += 1
        else:
            stats['sin_actividad'] += 1
        print(f'  {w.name:<30} limp={cleanings:<6} aten={cares:<6} -> {w.role}')
    db.session.commit()
    print(f'\nResultado: {stats}')


# ── TURNOS / CUADRANTES ─────────────────────────────────────────────────────

@app.cli.command('seed-shift-types')
def seed_shift_types():
    """Create default shift types (Mañana, Tarde, Noche)."""
    defaults = [
        {'name': 'Mañana', 'short_name': 'M', 'color': '#0d6efd', 'start_time': dt_time(7, 0), 'end_time': dt_time(15, 0), 'breaks_minutes': 30, 'sort_order': 1},
        {'name': 'Tarde', 'short_name': 'T', 'color': '#fd7e14', 'start_time': dt_time(15, 0), 'end_time': dt_time(22, 0), 'breaks_minutes': 30, 'sort_order': 2},
        {'name': 'Noche', 'short_name': 'N', 'color': '#6f42c1', 'start_time': dt_time(22, 0), 'end_time': dt_time(7, 0), 'breaks_minutes': 30, 'sort_order': 3},
    ]
    for d in defaults:
        if not ShiftType.query.filter_by(name=d['name']).first():
            db.session.add(ShiftType(**d))
            print(f'  Created: {d["name"]}')
        else:
            print(f'  Exists: {d["name"]}')
    db.session.commit()
    print('Done.')


@app.route('/cuadrantes')
@admin_required
def cuadrantes():
    import calendar
    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    group_id = request.args.get('group_id', '', type=str)

    first_day = date(year, month, 1)
    num_days = calendar.monthrange(year, month)[1]
    last_day = date(year, month, num_days)

    # Workers: exclude 'gestion' role, filter by group and role
    role_filter = request.args.get('role', '', type=str)
    query = Cleaner.query.filter(Cleaner.active == True, Cleaner.role.in_(['limpieza', 'atenciones', 'mixto']))
    if group_id:
        query = query.filter(Cleaner.groups.any(ResidentGroup.id == int(group_id)))
    if role_filter:
        query = query.filter(Cleaner.role == role_filter)
    workers = query.order_by(Cleaner.name).all()

    # Shift types
    shift_types = ShiftType.query.filter_by(active=True).order_by(ShiftType.sort_order).all()

    # Assignments for this month
    assignments = ShiftAssignment.query.filter(
        ShiftAssignment.date >= first_day,
        ShiftAssignment.date <= last_day,
    ).all()

    # Build lookup: {(cleaner_id, date_iso): assignment}
    assign_map = {}
    for a in assignments:
        assign_map[(a.cleaner_id, a.date.isoformat())] = a

    # Coverage summary: {day_iso: {shift_type_id: count}}
    coverage = {}
    for d in range(1, num_days + 1):
        day = date(year, month, d)
        day_iso = day.isoformat()
        coverage[day_iso] = {}
        for st in shift_types:
            coverage[day_iso][st.id] = 0
    for a in assignments:
        if a.shift_type_id and a.date.isoformat() in coverage:
            coverage[a.date.isoformat()][a.shift_type_id] = \
                coverage[a.date.isoformat()].get(a.shift_type_id, 0) + 1

    groups = ResidentGroup.query.order_by(ResidentGroup.name).all()

    # Absences for this month: {(cleaner_id, date_iso): AbsenceType}
    absences = Absence.query.options(
        joinedload(Absence.absence_type),
    ).filter(
        Absence.start_date <= last_day,
        Absence.end_date >= first_day,
    ).all()
    absence_map = {}
    for ab in absences:
        d = max(ab.start_date, first_day)
        while d <= min(ab.end_date, last_day):
            absence_map[(ab.cleaner_id, d.isoformat())] = ab.absence_type
            d += timedelta(days=1)

    # Worker shift configs (active ones)
    worker_configs = {c.cleaner_id: c for c in WorkerShiftConfig.query.filter(
        WorkerShiftConfig.effective_until.is_(None),
    ).options(joinedload(WorkerShiftConfig.pattern)).all()}

    # Navigation: prev/next month
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    return render_template('cuadrantes.html',
        year=year, month=month, num_days=num_days,
        workers=workers, shift_types=shift_types,
        assign_map=assign_map, coverage=coverage,
        absence_map=absence_map, worker_configs=worker_configs,
        coverage_reqs={f'{r.shift_type_id}_{r.day_type}': r.min_workers for r in ShiftCoverageRequirement.query.all()},
        patterns=RotationPattern.query.filter_by(active=True).order_by(RotationPattern.name).all(),
        groups=groups, group_id=group_id, role_filter=role_filter,
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
    )


@app.route('/cuadrantes/assign', methods=['POST'])
@admin_required
def cuadrantes_assign():
    """AJAX: assign or clear a shift for a worker on a date."""
    data = request.get_json()
    cleaner_id = data.get('cleaner_id')
    date_str = data.get('date')
    shift_type_id = data.get('shift_type_id')  # None or 0 = clear (libre)

    if not cleaner_id or not date_str:
        return jsonify({'error': 'Faltan datos'}), 400

    target_date = date.fromisoformat(date_str)
    existing = ShiftAssignment.query.filter_by(
        cleaner_id=cleaner_id, date=target_date
    ).first()

    if shift_type_id:
        st = db.session.get(ShiftType, int(shift_type_id))
        if not st:
            return jsonify({'error': 'Tipo de turno no válido'}), 400
        if existing:
            existing.shift_type_id = st.id
            existing.is_override = True
            existing.source = 'manual'
        else:
            existing = ShiftAssignment(
                cleaner_id=cleaner_id, date=target_date,
                shift_type_id=st.id, source='manual',
                created_by=current_user.id,
            )
            db.session.add(existing)
        db.session.commit()
        return jsonify({'ok': True, 'short_name': st.short_name, 'color': st.color})
    else:
        # Clear assignment (día libre)
        if existing:
            db.session.delete(existing)
            db.session.commit()
        return jsonify({'ok': True, 'short_name': '', 'color': ''})


@app.route('/cuadrantes/bulk-assign', methods=['POST'])
@admin_required
def cuadrantes_bulk_assign():
    """AJAX: copy a week or assign in bulk."""
    data = request.get_json()
    action = data.get('action')

    if action == 'copy_week':
        year = data.get('year')
        month = data.get('month')
        source_week_start = date.fromisoformat(data.get('source_start'))
        target_week_start = date.fromisoformat(data.get('target_start'))

        for offset in range(7):
            src_date = source_week_start + timedelta(days=offset)
            tgt_date = target_week_start + timedelta(days=offset)
            if tgt_date.month != month:
                continue
            # Get all assignments for source date
            sources = ShiftAssignment.query.filter_by(date=src_date).all()
            for sa in sources:
                existing = ShiftAssignment.query.filter_by(
                    cleaner_id=sa.cleaner_id, date=tgt_date
                ).first()
                if existing:
                    existing.shift_type_id = sa.shift_type_id
                    existing.is_override = True
                    existing.source = 'manual'
                else:
                    db.session.add(ShiftAssignment(
                        cleaner_id=sa.cleaner_id, date=tgt_date,
                        shift_type_id=sa.shift_type_id, source='manual',
                        created_by=current_user.id,
                    ))
        db.session.commit()
        return jsonify({'ok': True})

    return jsonify({'error': 'Acción no válida'}), 400


@app.route('/cuadrantes/clear', methods=['POST'])
@admin_required
def cuadrantes_clear():
    """Clear all shift assignments for a month."""
    data = request.get_json()
    year = data.get('year')
    month = data.get('month')
    import calendar
    num_days = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, num_days)
    deleted = ShiftAssignment.query.filter(
        ShiftAssignment.date >= first_day,
        ShiftAssignment.date <= last_day,
    ).delete()
    db.session.commit()
    return jsonify({'ok': True, 'deleted': deleted})


@app.route('/cuadrantes/export')
@admin_required
def cuadrantes_export():
    """Export the monthly shift grid to Excel."""
    import pandas as pd
    import io
    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    import calendar as cal_mod
    num_days = cal_mod.monthrange(year, month)[1]

    first_day = date(year, month, 1)
    last_day = date(year, month, num_days)

    workers = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    shift_types = {st.id: st for st in ShiftType.query.all()}
    assignments = ShiftAssignment.query.filter(
        ShiftAssignment.date >= first_day,
        ShiftAssignment.date <= last_day,
    ).all()

    assign_map = {}
    for a in assignments:
        assign_map[(a.cleaner_id, a.date.day)] = a

    # Build dataframe
    day_names_es = ['L', 'M', 'X', 'J', 'V', 'S', 'D']
    columns = ['Trabajador']
    for d in range(1, num_days + 1):
        dow = date(year, month, d).weekday()
        columns.append(f'{d} {day_names_es[dow]}')

    rows = []
    for w in workers:
        row = [w.name]
        for d in range(1, num_days + 1):
            a = assign_map.get((w.id, d))
            if a and a.shift_type_id and a.shift_type_id in shift_types:
                row.append(shift_types[a.shift_type_id].short_name)
            else:
                row.append('')
        rows.append(row)

    df = pd.DataFrame(rows, columns=columns)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name=f'Cuadrante {month:02d}-{year}')
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'cuadrante_{year}_{month:02d}.xlsx')


@app.route('/cuadrantes/manage-shift-types')
@admin_required
def manage_shift_types():
    shift_types = ShiftType.query.order_by(ShiftType.sort_order).all()
    return render_template('manage_shift_types.html', shift_types=shift_types)


@app.route('/shift-types/add_edit', methods=['POST'])
@admin_required
def add_edit_shift_type():
    st_id = request.form.get('shift_type_id', '').strip()
    name = request.form.get('name', '').strip()
    short_name = request.form.get('short_name', '').strip()
    color = request.form.get('color', '#0d6efd').strip()
    start_h, start_m = request.form.get('start_time', '07:00').split(':')
    end_h, end_m = request.form.get('end_time', '15:00').split(':')
    breaks_min = request.form.get('breaks_minutes', '0', type=int)
    sort_order = request.form.get('sort_order', '0', type=int)

    if not name or not short_name:
        flash('Nombre y abreviatura son obligatorios.', 'danger')
        return redirect(url_for('manage_shift_types'))

    start_t = dt_time(int(start_h), int(start_m))
    end_t = dt_time(int(end_h), int(end_m))

    if st_id:
        st = db.session.get(ShiftType, int(st_id))
        if st:
            st.name = name
            st.short_name = short_name
            st.color = color
            st.start_time = start_t
            st.end_time = end_t
            st.breaks_minutes = breaks_min
            st.sort_order = sort_order
            flash('Tipo de turno actualizado.', 'success')
    else:
        st = ShiftType(name=name, short_name=short_name, color=color,
                       start_time=start_t, end_time=end_t,
                       breaks_minutes=breaks_min, sort_order=sort_order)
        db.session.add(st)
        flash('Tipo de turno creado.', 'success')

    db.session.commit()
    return redirect(url_for('manage_shift_types'))


@app.route('/shift-types/delete/<int:id>', methods=['POST'])
@admin_required
def delete_shift_type(id):
    st = db.session.get(ShiftType, id)
    if st:
        if st.assignments:
            flash('No se puede eliminar: tiene asignaciones asociadas.', 'danger')
        else:
            db.session.delete(st)
            db.session.commit()
            flash('Tipo de turno eliminado.', 'success')
    return redirect(url_for('manage_shift_types'))


@app.route('/shift-types/toggle-active', methods=['POST'])
@admin_required
def toggle_shift_type_active():
    data = request.get_json()
    st = db.session.get(ShiftType, data.get('id'))
    if st:
        st.active = data.get('active', True)
        db.session.commit()
        return jsonify({'ok': True})
    return jsonify({'error': 'No encontrado'}), 404


# ── API WORKER: MIS TURNOS ──────────────────────────────────────────────────

@app.route('/api/worker/my-shifts')
@jwt_required()
def worker_my_shifts():
    """Return shift assignments for the authenticated worker."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'Worker not found'}), 404
    worker_id = worker.id

    month_str = request.args.get('month', '')
    if month_str:
        try:
            year, mo = month_str.split('-')
            year, mo = int(year), int(mo)
        except ValueError:
            return jsonify({'error': 'Invalid month format (YYYY-MM)'}), 400
    else:
        year, mo = datetime.now().year, datetime.now().month

    import calendar
    num_days = calendar.monthrange(year, mo)[1]
    first_day = date(year, mo, 1)
    last_day = date(year, mo, num_days)

    assignments = ShiftAssignment.query.filter(
        ShiftAssignment.cleaner_id == worker_id,
        ShiftAssignment.date >= first_day,
        ShiftAssignment.date <= last_day,
    ).order_by(ShiftAssignment.date).all()

    result = []
    for a in assignments:
        st = a.shift_type
        result.append({
            'date': a.date.isoformat(),
            'shift': {
                'name': st.name if st else 'Libre',
                'short_name': st.short_name if st else 'L',
                'color': st.color if st else '#dee2e6',
                'start_time': st.start_time.strftime('%H:%M') if st else None,
                'end_time': st.end_time.strftime('%H:%M') if st else None,
            } if st else None,
        })
    return jsonify({'year': year, 'month': mo, 'shifts': result})


# ─── Phase 2 & 3: Rotation Patterns, Absences, Validation ─────────────────────

@app.cli.command('seed-absence-types')
def seed_absence_types():
    defaults = [
        {'name': 'Vacaciones', 'short_name': 'VAC', 'color': '#198754', 'counts_as_worked': False},
        {'name': 'Baja médica', 'short_name': 'BAJ', 'color': '#dc3545', 'counts_as_worked': False},
        {'name': 'Asuntos propios', 'short_name': 'AP', 'color': '#ffc107', 'counts_as_worked': False},
        {'name': 'Permiso retribuido', 'short_name': 'PR', 'color': '#0dcaf0', 'counts_as_worked': True},
        {'name': 'Festivo', 'short_name': 'FES', 'color': '#6c757d', 'counts_as_worked': False},
    ]
    for d in defaults:
        if not AbsenceType.query.filter_by(name=d['name']).first():
            db.session.add(AbsenceType(**d))
            print(f'  Created: {d["name"]}')
        else:
            print(f'  Exists: {d["name"]}')
    db.session.commit()
    print('Done.')


@app.route('/cuadrantes/patrones')
@admin_required
def manage_patterns():
    patterns = RotationPattern.query.order_by(RotationPattern.name).all()
    shift_types = ShiftType.query.filter_by(active=True).order_by(ShiftType.sort_order).all()
    return render_template('manage_patterns.html', patterns=patterns, shift_types=shift_types)


@app.route('/cuadrantes/patrones/add_edit', methods=['POST'])
@admin_required
def add_edit_pattern():
    pattern_id = request.form.get('pattern_id', '').strip()
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    cycle_days = request.form.get('cycle_days', '7', type=int)

    if not name or cycle_days < 1:
        flash('Nombre y días del ciclo son obligatorios.', 'danger')
        return redirect(url_for('manage_patterns'))

    if pattern_id:
        pattern = db.session.get(RotationPattern, int(pattern_id))
        if pattern:
            pattern.name = name
            pattern.description = description
            pattern.cycle_days = cycle_days
            # Delete existing days and recreate
            RotationPatternDay.query.filter_by(pattern_id=pattern.id).delete()
    else:
        pattern = RotationPattern(name=name, description=description, cycle_days=cycle_days)
        db.session.add(pattern)
        db.session.flush()  # get pattern.id

    # Parse days from form: day_0, day_1, ..., day_N
    for d in range(cycle_days):
        st_id = request.form.get(f'day_{d}', '')
        db.session.add(RotationPatternDay(
            pattern_id=pattern.id,
            day_number=d,
            shift_type_id=int(st_id) if st_id else None,
        ))

    db.session.commit()
    flash('Patrón guardado correctamente.', 'success')
    return redirect(url_for('manage_patterns'))


@app.route('/cuadrantes/patrones/delete/<int:id>', methods=['POST'])
@admin_required
def delete_pattern(id):
    pattern = db.session.get(RotationPattern, id)
    if pattern:
        if pattern.worker_configs:
            flash('No se puede eliminar: hay trabajadores asignados a este patrón.', 'danger')
        else:
            db.session.delete(pattern)
            db.session.commit()
            flash('Patrón eliminado.', 'success')
    return redirect(url_for('manage_patterns'))


@app.route('/cuadrantes/worker-config', methods=['POST'])
@admin_required
def set_worker_shift_config():
    data = request.get_json()
    cleaner_id = data.get('cleaner_id')
    pattern_id = data.get('pattern_id')  # null = fixed shift
    fixed_shift_type_id = data.get('fixed_shift_type_id')  # null = pattern
    cycle_start_date_str = data.get('cycle_start_date')

    if not cleaner_id or not cycle_start_date_str:
        return jsonify({'error': 'Faltan datos'}), 400

    cycle_start = date.fromisoformat(cycle_start_date_str)

    # Deactivate previous configs
    existing = WorkerShiftConfig.query.filter(
        WorkerShiftConfig.cleaner_id == cleaner_id,
        WorkerShiftConfig.effective_until.is_(None),
    ).all()
    for e in existing:
        e.effective_until = date.today()

    config = WorkerShiftConfig(
        cleaner_id=cleaner_id,
        pattern_id=int(pattern_id) if pattern_id else None,
        fixed_shift_type_id=int(fixed_shift_type_id) if fixed_shift_type_id else None,
        cycle_start_date=cycle_start,
        effective_from=date.today(),
    )
    db.session.add(config)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/cuadrantes/generate', methods=['POST'])
@admin_required
def cuadrantes_generate():
    """Generate shift assignments using the SmartScheduler."""
    from .scheduler import SmartScheduler
    data = request.get_json()
    year = data.get('year')
    month = data.get('month')
    preserve_overrides = data.get('preserve_overrides', True)
    fill_gaps = data.get('fill_gaps', True)

    scheduler = SmartScheduler(year, month)
    result = scheduler.generate(
        preserve_overrides=preserve_overrides,
        fill_gaps=fill_gaps,
        created_by_id=current_user.id,
    )
    return jsonify(result)


@app.route('/cuadrantes/fill-gap', methods=['POST'])
@admin_required
def cuadrantes_fill_gap():
    """Fill a single coverage gap on a specific day/shift."""
    from .scheduler import SmartScheduler
    data = request.get_json()
    target_date = date.fromisoformat(data.get('date'))
    shift_type_id = data.get('shift_type_id')

    scheduler = SmartScheduler(target_date.year, target_date.month)
    # Load current schedule into memory
    for a in ShiftAssignment.query.filter(
        ShiftAssignment.date >= scheduler.first_day,
        ShiftAssignment.date <= scheduler.last_day,
    ).all():
        scheduler.schedule[(a.cleaner_id, a.date)] = a.shift_type_id
        if a.is_override:
            scheduler.overrides.add((a.cleaner_id, a.date))

    # Find best worker for this gap
    best_worker = None
    best_score = -1
    for w in scheduler.workers:
        key = (w.id, target_date)
        if key in scheduler.schedule and scheduler.schedule[key]:
            continue
        if key in scheduler.absent_days:
            continue
        if not scheduler._check_rest(w.id, target_date, shift_type_id):
            continue
        if scheduler._week_hours_with(w.id, target_date, shift_type_id) > 40:
            continue
        if scheduler._consecutive_days(w.id, target_date) >= 6:
            continue
        score = scheduler._fairness_score(w.id, target_date, shift_type_id)
        if score > best_score:
            best_score = score
            best_worker = w

    if best_worker:
        existing = ShiftAssignment.query.filter_by(
            cleaner_id=best_worker.id, date=target_date
        ).first()
        if existing:
            existing.shift_type_id = shift_type_id
            existing.source = 'smart'
        else:
            db.session.add(ShiftAssignment(
                cleaner_id=best_worker.id, date=target_date,
                shift_type_id=shift_type_id, source='smart',
                created_by=current_user.id,
            ))
        db.session.commit()
        st = db.session.get(ShiftType, shift_type_id)
        return jsonify({
            'ok': True,
            'worker_id': best_worker.id,
            'worker_name': best_worker.name,
            'short_name': st.short_name if st else '',
            'color': st.color if st else '',
        })
    return jsonify({'ok': False, 'error': 'No hay trabajadores disponibles para cubrir este turno.'}), 404


@app.route('/cuadrantes/coverage-settings')
@admin_required
def coverage_settings():
    shift_types = ShiftType.query.filter_by(active=True).order_by(ShiftType.sort_order).all()
    requirements = ShiftCoverageRequirement.query.all()
    req_map = {(r.shift_type_id, r.day_type): r for r in requirements}
    return render_template('coverage_settings.html', shift_types=shift_types, req_map=req_map)


@app.route('/cuadrantes/coverage-settings/save', methods=['POST'])
@admin_required
def save_coverage_settings():
    shift_types = ShiftType.query.filter_by(active=True).all()
    for st in shift_types:
        for day_type in ['weekday', 'weekend']:
            field_name = f'min_{st.id}_{day_type}'
            val = request.form.get(field_name, '', type=int)
            existing = ShiftCoverageRequirement.query.filter_by(
                shift_type_id=st.id, day_type=day_type
            ).first()
            if val and val > 0:
                if existing:
                    existing.min_workers = val
                else:
                    db.session.add(ShiftCoverageRequirement(
                        shift_type_id=st.id, day_type=day_type, min_workers=val,
                    ))
            elif existing:
                db.session.delete(existing)
    db.session.commit()
    flash('Requisitos de cobertura guardados.', 'success')
    return redirect(url_for('coverage_settings'))


@app.route('/cuadrantes/ausencias')
@admin_required
def manage_absences():
    month = request.args.get('month', datetime.now().month, type=int)
    year = request.args.get('year', datetime.now().year, type=int)

    absences = Absence.query.options(
        joinedload(Absence.cleaner),
        joinedload(Absence.absence_type),
    ).order_by(Absence.start_date.desc()).all()

    workers = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    absence_types = AbsenceType.query.filter_by(active=True).order_by(AbsenceType.name).all()

    return render_template('manage_absences.html',
        absences=absences, workers=workers, absence_types=absence_types,
        year=year, month=month)


@app.route('/cuadrantes/ausencias/add_edit', methods=['POST'])
@admin_required
def add_edit_absence():
    absence_id = request.form.get('absence_id', '').strip()
    cleaner_id = request.form.get('cleaner_id', type=int)
    absence_type_id = request.form.get('absence_type_id', type=int)
    start_date_str = request.form.get('start_date', '')
    end_date_str = request.form.get('end_date', '')
    notes = request.form.get('notes', '').strip()

    if not cleaner_id or not absence_type_id or not start_date_str or not end_date_str:
        flash('Todos los campos son obligatorios.', 'danger')
        return redirect(url_for('manage_absences'))

    start_d = date.fromisoformat(start_date_str)
    end_d = date.fromisoformat(end_date_str)

    if end_d < start_d:
        flash('La fecha fin debe ser posterior a la fecha inicio.', 'danger')
        return redirect(url_for('manage_absences'))

    if absence_id:
        absence = db.session.get(Absence, int(absence_id))
        if absence:
            absence.cleaner_id = cleaner_id
            absence.absence_type_id = absence_type_id
            absence.start_date = start_d
            absence.end_date = end_d
            absence.notes = notes
    else:
        absence = Absence(
            cleaner_id=cleaner_id,
            absence_type_id=absence_type_id,
            start_date=start_d,
            end_date=end_d,
            notes=notes,
            created_by=current_user.id,
        )
        db.session.add(absence)

    db.session.commit()
    flash('Ausencia registrada.', 'success')
    return redirect(url_for('manage_absences'))


@app.route('/cuadrantes/ausencias/delete/<int:id>', methods=['POST'])
@admin_required
def delete_absence(id):
    absence = db.session.get(Absence, id)
    if absence:
        db.session.delete(absence)
        db.session.commit()
        flash('Ausencia eliminada.', 'success')
    return redirect(url_for('manage_absences'))


@app.route('/cuadrantes/validate')
@admin_required
def cuadrantes_validate():
    """Validate shift assignments for labor law compliance. Returns warnings."""
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    import calendar
    num_days = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, num_days)

    # Get a few days before and after for cross-boundary checks
    check_start = first_day - timedelta(days=1)
    check_end = last_day + timedelta(days=1)

    workers = Cleaner.query.filter_by(active=True).all()
    shift_types = {st.id: st for st in ShiftType.query.all()}

    assignments = ShiftAssignment.query.filter(
        ShiftAssignment.date >= check_start,
        ShiftAssignment.date <= check_end,
    ).all()

    # Build lookup: {cleaner_id: {date: assignment}}
    assign_map = {}
    for a in assignments:
        if a.cleaner_id not in assign_map:
            assign_map[a.cleaner_id] = {}
        assign_map[a.cleaner_id][a.date] = a

    # Get absences for the month
    absences = Absence.query.filter(
        Absence.start_date <= last_day,
        Absence.end_date >= first_day,
    ).all()
    absence_map = {}  # {cleaner_id: set of dates}
    for ab in absences:
        if ab.cleaner_id not in absence_map:
            absence_map[ab.cleaner_id] = set()
        d = max(ab.start_date, first_day)
        while d <= min(ab.end_date, last_day):
            absence_map[ab.cleaner_id].add(d)
            d += timedelta(days=1)

    warnings = []

    for worker in workers:
        wid = worker.id
        worker_assignments = assign_map.get(wid, {})
        worker_absences = absence_map.get(wid, set())

        # --- Check 1: Minimum 12h rest between consecutive shifts ---
        prev_assignment = worker_assignments.get(check_start)
        for d in range(1, num_days + 1):
            current_date = date(year, month, d)
            current = worker_assignments.get(current_date)

            if prev_assignment and current and prev_assignment.shift_type_id and current.shift_type_id:
                if current_date not in worker_absences:
                    prev_st = shift_types.get(prev_assignment.shift_type_id)
                    curr_st = shift_types.get(current.shift_type_id)
                    if prev_st and curr_st:
                        # Calculate hours between end of prev shift and start of current
                        prev_end = datetime.combine(prev_assignment.date, prev_st.end_time)
                        if prev_st.end_time <= prev_st.start_time:
                            prev_end += timedelta(days=1)
                        curr_start = datetime.combine(current_date, curr_st.start_time)
                        rest_hours = (curr_start - prev_end).total_seconds() / 3600
                        if rest_hours < 12 and rest_hours >= 0:
                            warnings.append({
                                'type': 'rest',
                                'worker_id': wid,
                                'worker_name': worker.name,
                                'date': current_date.isoformat(),
                                'message': f'Solo {rest_hours:.0f}h de descanso entre turnos (mínimo 12h)',
                            })

            prev_assignment = current

        # --- Check 2: Weekly hours (max 40h) ---
        checked_weeks = set()
        for d in range(1, num_days + 1):
            current_date = date(year, month, d)
            iso_year, iso_week, _ = current_date.isocalendar()
            week_key = (iso_year, iso_week)
            if week_key in checked_weeks:
                continue
            checked_weeks.add(week_key)

            # Calculate total hours for this week
            week_hours = 0.0
            monday = current_date - timedelta(days=current_date.weekday())
            for wd in range(7):
                week_date = monday + timedelta(days=wd)
                wa = worker_assignments.get(week_date)
                if wa and wa.shift_type_id and week_date not in worker_absences:
                    st = shift_types.get(wa.shift_type_id)
                    if st:
                        start_dt = datetime.combine(week_date, st.start_time)
                        end_dt = datetime.combine(week_date, st.end_time)
                        if end_dt <= start_dt:
                            end_dt += timedelta(days=1)
                        hours = ((end_dt - start_dt).total_seconds() / 3600) - ((st.breaks_minutes or 0) / 60)
                        week_hours += hours

            if week_hours > 40:
                sunday = monday + timedelta(days=6)
                warnings.append({
                    'type': 'hours',
                    'worker_id': wid,
                    'worker_name': worker.name,
                    'date': monday.isoformat(),
                    'message': f'{week_hours:.1f}h en semana {monday.strftime("%d/%m")}-{sunday.strftime("%d/%m")} (máximo 40h)',
                })

        # --- Check 3: Weekly rest (at least 1.5 consecutive days off per week) ---
        checked_rest_weeks = set()
        for d in range(1, num_days + 1):
            current_date = date(year, month, d)
            iso_year, iso_week, _ = current_date.isocalendar()
            week_key = (iso_year, iso_week)
            if week_key in checked_rest_weeks:
                continue
            checked_rest_weeks.add(week_key)

            monday = current_date - timedelta(days=current_date.weekday())

            has_work_every_day = all(
                worker_assignments.get(monday + timedelta(days=wd))
                and worker_assignments.get(monday + timedelta(days=wd)).shift_type_id
                and (monday + timedelta(days=wd)) not in worker_absences
                for wd in range(7)
            )

            if has_work_every_day:
                sunday = monday + timedelta(days=6)
                warnings.append({
                    'type': 'weekly_rest',
                    'worker_id': wid,
                    'worker_name': worker.name,
                    'date': monday.isoformat(),
                    'message': f'Sin día libre en semana {monday.strftime("%d/%m")}-{sunday.strftime("%d/%m")}',
                })

    return jsonify({'warnings': warnings})


# ── RUTAS DE LIMPIEZA INTELIGENTES ──────────────────────────────────────────

def _compute_cleaning_stats(days_back: int = 90) -> dict:
    """Compute average cleaning duration per room from historical data."""
    cutoff = datetime.now() - timedelta(days=days_back)
    records = CleaningRecord.query.filter(
        CleaningRecord.end_time.isnot(None),
        CleaningRecord.start_time >= cutoff,
    ).all()

    # Group by room_id -> list of durations in minutes
    room_durations: dict[int, list[float]] = {}
    # Track historical sequences: per worker per day -> ordered list of room_ids
    worker_sequences: dict[tuple[int, str], list[tuple[float, int]]] = {}

    for r in records:
        dur = r.calculate_duration()
        if dur and dur > 60 and dur < 7200:  # between 1 min and 2 hours (filter outliers)
            mins = dur / 60
            room_durations.setdefault(r.room_id, []).append(mins)
            day_key = (r.cleaner_id, r.start_time.strftime('%Y-%m-%d'))
            worker_sequences.setdefault(day_key, []).append((r.start_time.timestamp(), r.room_id))

    # Average per room
    avg_per_room = {}
    for room_id, durs in room_durations.items():
        avg_per_room[room_id] = round(sum(durs) / len(durs), 1)

    # Most common sequence patterns: count how often room A is followed by room B
    transition_counts: dict[tuple[int, int], int] = {}
    for day_key, seq in worker_sequences.items():
        seq.sort()  # sort by timestamp
        room_order = [room_id for _, room_id in seq]
        for i in range(len(room_order) - 1):
            pair = (room_order[i], room_order[i + 1])
            transition_counts[pair] = transition_counts.get(pair, 0) + 1

    return {
        'avg_per_room': avg_per_room,
        'transition_counts': transition_counts,
        'room_durations': room_durations,
    }


@app.route('/api/worker/cleaning-route')
@jwt_required()
def worker_cleaning_route():
    """Return suggested cleaning route for the authenticated worker."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'Worker not found'}), 404

    # Check if worker has a shift assigned today
    today = date.today()
    today_shift = ShiftAssignment.query.filter_by(cleaner_id=worker.id, date=today).first()
    has_shift = today_shift and today_shift.shift_type_id is not None

    # Check if worker is absent today
    from sqlalchemy import and_
    is_absent = Absence.query.filter(
        Absence.cleaner_id == worker.id,
        Absence.start_date <= today,
        Absence.end_date >= today,
    ).first() is not None

    if is_absent:
        return jsonify({'route': [], 'summary': {
            'total_rooms': 0, 'cleaned_today': 0, 'remaining': 0,
            'total_estimated_minutes': 0, 'remaining_estimated_minutes': 0,
            'status': 'absent', 'message': 'Hoy tienes ausencia registrada.',
        }})

    if not has_shift:
        return jsonify({'route': [], 'summary': {
            'total_rooms': 0, 'cleaned_today': 0, 'remaining': 0,
            'total_estimated_minutes': 0, 'remaining_estimated_minutes': 0,
            'status': 'no_shift', 'message': 'No tienes turno asignado hoy.',
        }})

    # Determine rooms: manual zone assignment OR auto-detect from history
    zone_assignments = CleaningZoneAssignment.query.filter_by(cleaner_id=worker.id).all()
    assigned_floor_ids = {za.floor_id for za in zone_assignments}

    if assigned_floor_ids:
        # Manual assignment exists: use it
        rooms = Room.query.filter(Room.floor_id.in_(assigned_floor_ids)).all()
    else:
        # Auto-detect from historical data: which rooms has this worker cleaned?
        cutoff_auto = datetime.now() - timedelta(days=90)
        worker_room_ids = {r[0] for r in db.session.query(CleaningRecord.room_id).filter(
            CleaningRecord.cleaner_id == worker.id,
            CleaningRecord.end_time.isnot(None),
            CleaningRecord.start_time >= cutoff_auto,
        ).distinct().all()}

        if worker_room_ids:
            # Show rooms this worker usually cleans
            rooms = Room.query.filter(Room.id.in_(worker_room_ids)).all()
        else:
            # No history: show all rooms
            rooms = Room.query.all()

    # Get today's completed cleanings
    today_start = datetime.combine(date.today(), datetime.min.time())
    today_cleaned = {r.room_id for r in CleaningRecord.query.filter(
        CleaningRecord.end_time.isnot(None),
        CleaningRecord.start_time >= today_start,
    ).all()}

    # Get historical stats
    stats = _compute_cleaning_stats(90)
    avg_per_room = stats['avg_per_room']

    # Get target times per room type as fallback
    targets = {t.room_type_id: t.target_minutes for t in CleaningTargetTime.query.all()}

    # Calculate cleaning frequency and urgency per room
    cutoff_freq = datetime.now() - timedelta(days=90)
    all_records = CleaningRecord.query.filter(
        CleaningRecord.end_time.isnot(None),
        CleaningRecord.start_time >= cutoff_freq,
    ).all()
    room_clean_count = {}
    room_last_cleaned = {}
    for rec in all_records:
        room_clean_count[rec.room_id] = room_clean_count.get(rec.room_id, 0) + 1
        if rec.room_id not in room_last_cleaned or rec.start_time > room_last_cleaned[rec.room_id]:
            room_last_cleaned[rec.room_id] = rec.start_time

    now = datetime.now()

    # Build room info with estimated times and urgency
    route = []
    for room in rooms:
        est_minutes = avg_per_room.get(room.id, targets.get(room.room_type_id, 15))
        cleaned = room.id in today_cleaned
        floor = room.floor

        # Calculate urgency: how overdue is this room?
        count = room_clean_count.get(room.id, 0)
        last = room_last_cleaned.get(room.id)
        if count > 0:
            expected_freq_days = 90 / count  # how often it's usually cleaned
        else:
            expected_freq_days = 7  # default: weekly

        if last:
            days_since = (now - last).days
            # Urgency ratio: >1 means overdue, >2 means very overdue
            urgency = days_since / expected_freq_days if expected_freq_days > 0 else 0
        else:
            days_since = None
            urgency = 10  # never cleaned = highest urgency

        # Priority label
        if cleaned:
            priority = 'done'
            priority_label = 'Limpiada hoy'
        elif urgency >= 2:
            priority = 'urgent'
            priority_label = f'Atrasada ({days_since}d sin limpiar, se limpia cada {expected_freq_days:.0f}d)'
        elif urgency >= 1:
            priority = 'due'
            priority_label = f'Toca hoy ({days_since}d sin limpiar)'
        else:
            priority = 'ok'
            remaining_days = expected_freq_days - (days_since or 0)
            priority_label = f'Faltan {remaining_days:.0f}d para la siguiente'

        route.append({
            'id': room.id,
            'number': room.number,
            'description': room.description or '',
            'room_type': room.room_type.name if room.room_type else '',
            'floor_name': floor.name if floor else '',
            'floor_id': floor.id if floor else 0,
            'estimated_minutes': round(est_minutes, 1),
            'cleaned_today': cleaned,
            'days_since_cleaned': days_since,
            'expected_frequency': round(expected_freq_days, 1),
            'urgency': round(urgency, 2),
            'priority': priority,
            'priority_label': priority_label,
        })

    # Sort: urgent first, then due, then by floor+number for the rest
    def route_sort_key(r):
        priority_order = {'urgent': 0, 'due': 1, 'ok': 2, 'done': 3}
        p = priority_order.get(r['priority'], 2)
        try:
            num = (0, int(r['number']))
        except (ValueError, TypeError):
            num = (1, r['number'])
        return (p, r['floor_name'], num)

    route.sort(key=route_sort_key)

    total_estimated = sum(r['estimated_minutes'] for r in route)
    remaining_estimated = sum(r['estimated_minutes'] for r in route if not r['cleaned_today'])
    cleaned_count = sum(1 for r in route if r['cleaned_today'])
    urgent_count = sum(1 for r in route if r['priority'] == 'urgent')
    due_count = sum(1 for r in route if r['priority'] == 'due')

    return jsonify({
        'route': route,
        'summary': {
            'total_rooms': len(route),
            'cleaned_today': cleaned_count,
            'remaining': len(route) - cleaned_count,
            'urgent': urgent_count,
            'due': due_count,
            'total_estimated_minutes': round(total_estimated, 0),
            'remaining_estimated_minutes': round(remaining_estimated, 0),
            'status': 'ok',
            'shift': {
                'name': today_shift.shift_type.name,
                'short_name': today_shift.shift_type.short_name,
                'start_time': today_shift.shift_type.start_time.strftime('%H:%M'),
                'end_time': today_shift.shift_type.end_time.strftime('%H:%M'),
            } if today_shift and today_shift.shift_type else None,
        },
    })


@app.route('/admin/cleaning-analytics')
@admin_required
def admin_cleaning_analytics():
    days = request.args.get('days', 30, type=int)
    stats = _compute_cleaning_stats(days)
    avg_per_room = stats['avg_per_room']
    room_durations = stats['room_durations']

    # Room details with averages
    rooms = Room.query.options(
        joinedload(Room.floor), joinedload(Room.room_type)
    ).all()
    room_map = {r.id: r for r in rooms}

    room_stats = []
    for room_id, avg_min in sorted(avg_per_room.items(), key=lambda x: -x[1]):
        room = room_map.get(room_id)
        if room:
            room_stats.append({
                'room_number': room.number,
                'description': room.description or '',
                'floor': room.floor.name if room.floor else '',
                'room_type': room.room_type.name if room.room_type else '',
                'avg_minutes': avg_min,
                'count': len(room_durations.get(room_id, [])),
            })

    # Per room type
    type_stats = {}
    for room_id, durs in room_durations.items():
        room = room_map.get(room_id)
        if room and room.room_type:
            tname = room.room_type.name
            type_stats.setdefault(tname, []).extend(durs)
    type_averages = [
        {'type': tname, 'avg': round(sum(durs) / len(durs), 1), 'count': len(durs)}
        for tname, durs in sorted(type_stats.items())
    ]

    # Per worker
    cutoff = datetime.now() - timedelta(days=days)
    worker_records = CleaningRecord.query.filter(
        CleaningRecord.end_time.isnot(None),
        CleaningRecord.start_time >= cutoff,
    ).options(joinedload(CleaningRecord.cleaner)).all()

    worker_stats_map = {}
    for r in worker_records:
        dur = r.calculate_duration()
        if dur and 60 < dur < 7200:
            wid = r.cleaner_id
            worker_stats_map.setdefault(wid, {'name': r.cleaner.name if r.cleaner else '?', 'durations': []})
            worker_stats_map[wid]['durations'].append(dur / 60)

    worker_stats = [
        {'name': ws['name'], 'avg': round(sum(ws['durations']) / len(ws['durations']), 1),
         'count': len(ws['durations']), 'total_hours': round(sum(ws['durations']) / 60, 1)}
        for ws in sorted(worker_stats_map.values(), key=lambda x: -sum(x['durations']) / len(x['durations']))
    ]

    # Coverage gaps: rooms not cleaned in last 7 days
    week_ago = datetime.now() - timedelta(days=7)
    last_cleaned = {}
    for r in CleaningRecord.query.filter(CleaningRecord.end_time.isnot(None)).all():
        if r.room_id not in last_cleaned or r.start_time > last_cleaned[r.room_id]:
            last_cleaned[r.room_id] = r.start_time

    coverage_gaps = []
    for room in rooms:
        last = last_cleaned.get(room.id)
        if not last or last < week_ago:
            days_since = (datetime.now() - last).days if last else None
            coverage_gaps.append({
                'room_number': room.number,
                'description': room.description or '',
                'floor': room.floor.name if room.floor else '',
                'last_cleaned': last.strftime('%d/%m/%Y') if last else 'Nunca',
                'days_since': days_since,
            })

    targets = {t.room_type_id: t.target_minutes for t in CleaningTargetTime.query.all()}
    target_map = {}
    for rt in RoomType.query.all():
        if rt.id in targets:
            target_map[rt.name] = targets[rt.id]

    return render_template('admin_cleaning_analytics.html',
        days=days, room_stats=room_stats, type_averages=type_averages,
        worker_stats=worker_stats, coverage_gaps=coverage_gaps,
        target_map=target_map,
    )


@app.route('/admin/cleaning-config')
@admin_required
def admin_cleaning_config():
    workers = Cleaner.query.filter(Cleaner.active == True, Cleaner.role.in_(['limpieza', 'mixto'])).order_by(Cleaner.name).all()
    floors = Floor.query.order_by(Floor.name).all()
    room_types = RoomType.query.all()
    assignments = CleaningZoneAssignment.query.all()
    targets = {t.room_type_id: t.target_minutes for t in CleaningTargetTime.query.all()}
    assign_map = {}
    for a in assignments:
        assign_map.setdefault(a.cleaner_id, set()).add(a.floor_id)
    return render_template('admin_cleaning_config.html',
        workers=workers, floors=floors, room_types=room_types,
        assign_map=assign_map, targets=targets,
    )


@app.route('/admin/cleaning-config/save-assignments', methods=['POST'])
@admin_required
def save_cleaning_assignments():
    cleaner_id = request.form.get('cleaner_id', type=int)
    if not cleaner_id:
        flash('Selecciona un trabajador.', 'danger')
        return redirect(url_for('admin_cleaning_config'))
    floor_ids = request.form.getlist('floor_ids')
    CleaningZoneAssignment.query.filter_by(cleaner_id=cleaner_id).delete()
    for fid in floor_ids:
        db.session.add(CleaningZoneAssignment(cleaner_id=cleaner_id, floor_id=int(fid)))
    db.session.commit()
    flash('Zonas asignadas correctamente.', 'success')
    return redirect(url_for('admin_cleaning_config'))


@app.route('/admin/cleaning-config/save-targets', methods=['POST'])
@admin_required
def save_cleaning_targets():
    room_types = RoomType.query.all()
    for rt in room_types:
        val = request.form.get(f'target_{rt.id}', type=float)
        existing = CleaningTargetTime.query.filter_by(room_type_id=rt.id).first()
        if val and val > 0:
            if existing:
                existing.target_minutes = val
            else:
                db.session.add(CleaningTargetTime(room_type_id=rt.id, target_minutes=val))
        elif existing:
            db.session.delete(existing)
    db.session.commit()
    flash('Tiempos objetivo guardados.', 'success')
    return redirect(url_for('admin_cleaning_config'))
