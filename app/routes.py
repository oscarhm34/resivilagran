from __future__ import annotations
from . import app, db, limiter
from .models import (Cleaner, Room, CleaningRecord, Floor, RoomType, Resident,
                      CareType, CareRecord, ResidentGroup, cleaner_groups,
                      WorkerSelfie, LegalDocument, DocumentSignature,
                      TrainingPill, TrainingQuestion, TrainingCompletion,
                      ChecklistItem, ResidentDocument,
                      VitalSignType, VitalSignReading, AppSetting)
from flask import request, jsonify, render_template, redirect, url_for, flash, send_file, send_from_directory, abort
from flask_login import login_user, logout_user, login_required, current_user
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from werkzeug.utils import secure_filename as _secure_filename

ALLOWED_DOC_EXTENSIONS = {'pdf', 'txt', 'csv', 'doc', 'docx', 'jpg', 'jpeg', 'png', 'webp'}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}


def _allowed_file(filename: str, allowed: set) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed
from datetime import datetime, timedelta
from sqlalchemy.orm import joinedload
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
@login_required
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

    limpiadas_ids = [
        r[0] for r in db.session.query(CleaningRecord.room_id)
        .filter(CleaningRecord.end_time.isnot(None))
        .distinct()
        .all()
    ]
    if limpiadas_ids:
        habitaciones_sin_limpiar = Room.query.filter(~Room.id.in_(limpiadas_ids)).count()
    else:
        habitaciones_sin_limpiar = Room.query.count()

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
        access_token = create_access_token(identity=username)
        return jsonify(access_token=access_token, id_cleaner=user.id, cleaner_name=user.name), 200

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
@login_required
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
@login_required
def add_edit_cleaner():
    cleaner_id = request.form.get('cleaner_id')
    username = request.form.get('username', '').strip()
    name = request.form.get('name', '').strip()
    password = request.form.get('password', '')
    is_admin = bool(request.form.get('is_admin'))
    active = bool(request.form.get('active'))

    group_ids = request.form.getlist('group_ids')
    selected_groups = ResidentGroup.query.filter(ResidentGroup.id.in_(group_ids)).all() if group_ids else []

    if cleaner_id:
        cleaner = db.session.get(Cleaner, int(cleaner_id))
        if cleaner:
            cleaner.username = username
            cleaner.name = name
            cleaner.is_admin = is_admin
            cleaner.active = active
            cleaner.groups = selected_groups
            if password:
                cleaner.set_password(password)
            db.session.commit()
            flash('Trabajador actualizado correctamente.', 'success')
        else:
            flash('Trabajador no encontrado.', 'error')
    else:
        new_cleaner = Cleaner(username=username, name=name, is_admin=is_admin, active=active)
        new_cleaner.set_password(password)
        new_cleaner.groups = selected_groups
        db.session.add(new_cleaner)
        db.session.commit()
        flash('Trabajador añadido correctamente.', 'success')

    return redirect(url_for('manage_workers'))


@app.route('/cleaners/delete/<int:id>', methods=['POST'])
@login_required
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
@login_required
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
@login_required
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
@login_required
def manage_cleaning_zones():
    rooms = Room.query.all()
    floors = Floor.query.all()
    room_types = RoomType.query.all()
    return render_template(
        'manage_cleaning_zones.html',
        rooms=rooms, floors=floors, room_types=room_types, form_data={}
    )


@app.route('/rooms/add_edit', methods=['POST'])
@login_required
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
@login_required
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
@login_required
def manage_room_types():
    room_types = RoomType.query.all()
    return render_template('manage_room_types.html', room_types=room_types)


@app.route('/room_types/add_edit', methods=['POST'])
@login_required
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
@login_required
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
@login_required
def manage_floors():
    floors = Floor.query.all()
    return render_template('manage_floors.html', floors=floors)


@app.route('/floors/add_edit', methods=['POST'])
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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

    worker = db.session.get(Cleaner, worker_id)
    if not worker:
        return jsonify({'groups': []}), 200

    today = datetime.now().date()
    hoy_inicio = datetime.combine(today, datetime.min.time())
    hoy_fin = datetime.combine(today + timedelta(days=1), datetime.min.time())

    # Single query: all completed care records today for residents in worker's groups
    group_ids = [g.id for g in worker.groups]
    resident_ids_in_groups = [r.id for r in Resident.query.filter(
        Resident.group_id.in_(group_ids), Resident.active == True
    ).all()] if group_ids else []

    from collections import defaultdict
    care_by_resident: dict[int, list] = defaultdict(list)
    if resident_ids_in_groups:
        all_care = CareRecord.query.options(
            db.joinedload(CareRecord.care_types),
            db.joinedload(CareRecord.care_type),
        ).filter(
            CareRecord.resident_id.in_(resident_ids_in_groups),
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
        for r in Resident.query.filter_by(group_id=group.id, active=True).order_by(Resident.name).all():
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
@login_required
def manage_residents():
    estado = request.args.get('estado', 'altas')
    query = Resident.query.order_by(Resident.name)
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
def manage_checklist():
    items = ChecklistItem.query.order_by(ChecklistItem.sort_order, ChecklistItem.id).all()
    return render_template('manage_checklist.html', items=items)


@app.route('/checklist/add_edit', methods=['POST'])
@login_required
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
@login_required
def delete_checklist_item(id: int):
    item = db.session.get(ChecklistItem, id)
    if item:
        db.session.delete(item)
        db.session.commit()
        flash('Item eliminado.', 'success')
    return redirect(url_for('manage_checklist'))


@app.route('/checklist/toggle-active', methods=['POST'])
@login_required
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
@login_required
def manage_groups():
    groups = ResidentGroup.query.order_by(ResidentGroup.name).all()
    return render_template('manage_groups.html', groups=groups)


@app.route('/groups/<int:id>')
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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


@app.route('/admin/care-record/<int:record_id>/delete', methods=['POST'])
@login_required
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
@login_required
def admin_documents():
    docs = LegalDocument.query.order_by(LegalDocument.created_at.desc()).all()
    workers = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    return render_template('admin_documents.html', documents=docs, workers=workers)


@app.route('/admin/documents/create', methods=['POST'])
@login_required
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
@login_required
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
@login_required
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
@login_required
def toggle_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    doc.active = not doc.active
    db.session.commit()
    flash(f'Documento {"activado" if doc.active else "desactivado"}.', 'success')
    return redirect(url_for('admin_documents'))


@app.route('/admin/documents/<int:doc_id>/signatures')
@login_required
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
@login_required
def admin_help():
    return render_template('admin_help.html')


@app.route('/admin/analytics')
@login_required
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
@login_required
def admin_settings():
    if request.method == 'POST':
        AppSetting.set('allow_group_care', 'true' if request.form.get('allow_group_care') else 'false')
        AppSetting.set('nfc_only', 'true' if request.form.get('nfc_only') else 'false')
        flash('Configuración guardada.', 'success')
        return redirect(url_for('admin_settings'))
    return render_template('admin_settings.html',
        allow_group_care=AppSetting.get('allow_group_care', 'true') == 'true',
        nfc_only=AppSetting.get('nfc_only', 'true') == 'true',
    )


@app.route('/api/config')
@jwt_required()
def api_config():
    return jsonify({
        'allow_group_care': AppSetting.get('allow_group_care', 'true') == 'true',
        'nfc_only': AppSetting.get('nfc_only', 'true') == 'true',
    }), 200


@app.route('/admin/training')
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
def toggle_training(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    pill.active = not pill.active
    db.session.commit()
    flash(f'Píldora {"activada" if pill.active else "desactivada"}.', 'success')
    return redirect(url_for('admin_training'))


@app.route('/admin/training/<int:pill_id>/results')
@login_required
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
