"""NFC, Worker API, and file-serving endpoints."""
from __future__ import annotations

from flask import (
    Blueprint, request, jsonify, render_template, redirect, url_for,
    flash, send_from_directory, abort, current_app,
)
from flask_login import login_required
from flask_jwt_extended import (
    create_access_token, jwt_required, get_jwt_identity,
)
from datetime import datetime, timedelta
from sqlalchemy.orm import joinedload, subqueryload
from collections import defaultdict
import json as _json
import time
import base64
import os

from .. import app, db, limiter
from ..models import (
    Cleaner, Room, Floor, Resident, CareType, CareRecord, CleaningRecord,
    ResidentGroup, cleaner_groups, ChecklistItem, WorkerSelfie,
    VitalSignType, VitalSignReading, AppSetting, Notification,
    MoodRecord, MealRecord,
)
from ..utils import (
    admin_required, _verify_worker_id, _current_worker_id, _safe_commit,
    _check_single_session_conflict, _resolve_nfc_code, _format_duration,
    _allowed_file, ALLOWED_IMAGE_EXTENSIONS,
)

bp = Blueprint('nfc', __name__)


# ── HELPER ──────────────────────────────────────────────────────────────────

def _save_base64_photo(b64_data: str, subfolder: str, cleaner_id: int) -> str:
    """Decodifica base64 (data URI o raw), re-processa com a JPEG via Pillow i retorna el path relatiu."""
    from PIL import Image
    from io import BytesIO
    if ',' in b64_data:
        b64_data = b64_data.split(',', 1)[1]
    try:
        img_bytes = base64.b64decode(b64_data)
    except Exception:
        raise ValueError('Imagen base64 no válida.')
    try:
        img = Image.open(BytesIO(img_bytes))
        img = img.convert('RGB')
        img.thumbnail((800, 800))
        ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        filename = f'{cleaner_id}_{ts}.jpg'
        folder = os.path.join(app.config['UPLOAD_FOLDER'], subfolder)
        os.makedirs(folder, exist_ok=True)
        filepath = os.path.join(folder, filename)
        img.save(filepath, 'JPEG', quality=85, optimize=True)
    except (OSError, IOError) as e:
        app.logger.error('Error saving photo: %s', e)
        raise ValueError('No se pudo guardar la foto.')
    return f'{subfolder}/{filename}'


# ── API – APP MÓVIL (sin autenticación web, usan JWT) ────────────────────────

@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10/minute", methods=["POST"])
def login():
    """Endpoint de autenticación para la app Android – devuelve JWT."""
    if request.method == 'GET':
        return redirect(url_for('admin_bp.admin_login'))

    username = request.form.get('username') or (request.json or {}).get('username')
    password = request.form.get('password') or (request.json or {}).get('password')

    user = Cleaner.query.filter_by(username=username).first()
    if user and user.check_password(password):
        access_token = create_access_token(identity=username, expires_delta=timedelta(days=7))
        return jsonify(access_token=access_token, id_cleaner=user.id, cleaner_name=user.name, role=user.role), 200

    return jsonify({'error': 'Credenciales incorrectas'}), 401


@bp.route('/api/nfc/refresh-token', methods=['POST'])
@jwt_required()
def refresh_token():
    identity = get_jwt_identity()
    new_token = create_access_token(identity=identity, expires_delta=timedelta(days=7))
    return jsonify(access_token=new_token), 200


@bp.route('/start_cleaning', methods=['POST'])
@jwt_required()
def start_cleaning():
    data = request.json or {}
    # El cleaner es siempre el del token: el `cleaner_id` que manda el cliente
    # se ignora para que nadie pueda abrir una limpieza a nombre de otra.
    cleaner_id = _current_worker_id()
    room_number = data.get('room_id')
    if not cleaner_id:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
    if not room_number:
        return jsonify({'error': 'Campo requerido: room_id'}), 400

    room = Room.query.filter_by(number=room_number).first()
    if not room:
        return jsonify({'error': 'Habitación no encontrada'}), 404

    active_cleaning = CleaningRecord.query.filter_by(
        cleaner_id=cleaner_id, room_id=room.id, end_time=None
    ).first()

    if active_cleaning:
        active_cleaning.end_time = datetime.now()
        ok, err = _safe_commit()
        if not ok:
            return jsonify({'error': err}), 500
        return jsonify({
            'message': f'Limpieza {active_cleaning.id} finalizada en habitación {room_number}.'
        }), 200

    # Check single session restriction
    if _check_single_session_conflict(cleaner_id):
        return jsonify({'error': 'Ya tienes una sesión activa. Finalízala antes de iniciar otra.'}), 409

    new_record = CleaningRecord(cleaner_id=cleaner_id, room_id=room.id, start_time=datetime.now())
    db.session.add(new_record)
    ok, err = _safe_commit()
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({
        'message': f'Limpieza {new_record.id} iniciada en habitación {room_number}.',
        'record_id': new_record.id,
    }), 200


@bp.route('/end_cleaning', methods=['POST'])
@jwt_required()
def end_cleaning():
    data = request.json or {}
    record_id = data.get('record_id')
    if not record_id:
        return jsonify({'error': 'Campo requerido: record_id'}), 400
    record = db.session.get(CleaningRecord, record_id)
    if not record or record.end_time:
        return jsonify({'error': 'Registro no válido o limpieza ya finalizada.'}), 400
    if record.cleaner_id != _current_worker_id():
        return jsonify({'error': 'No autorizado'}), 403
    record.end_time = datetime.now()
    ok, err = _safe_commit()
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'message': 'Limpieza finalizada.', 'duration': record.calculate_duration()}), 200


@bp.route('/check_cleaning', methods=['GET'])
@jwt_required()
def check_cleaning():
    cleaner_id = _current_worker_id()
    if not cleaner_id:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
    record = CleaningRecord.query.filter_by(cleaner_id=cleaner_id, end_time=None).first()
    if record:
        return jsonify({'room_id': record.room_id}), 200
    return jsonify({'message': 'No hay limpiezas en curso.'}), 200


@bp.route('/cleaning_summary/<int:cleaner_id>', methods=['GET'])
@jwt_required()
def cleaning_summary(cleaner_id: int):
    if _verify_worker_id(cleaner_id) is None:
        return jsonify({'error': 'No autorizado'}), 403
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


@bp.route('/api/registros-limpieza', methods=['GET'])
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


# ── WORKER WEBAPP ─────────────────────────────────────────────────────────────

@bp.route('/worker')
def worker():
    return render_template('worker.html')


@bp.route('/sw.js')
def service_worker():
    return send_from_directory(
        current_app.static_folder, 'sw.js',
        mimetype='application/javascript',
    )


@bp.route('/worker/manifest.json')
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

@bp.route('/api/care-types')
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


@bp.route('/api/debug/record')
@admin_required
def debug_record():
    """Diagnóstico: comprueba un registro por ID. Solo admin. /api/debug/record?id=X&mode=cleaning"""
    if os.getenv('FLASK_DEBUG') != '1':
        abort(404)
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


@bp.route('/api/worker/active-sessions')
@jwt_required()
def worker_active_sessions():
    worker_id = _current_worker_id()
    if not worker_id:
        return jsonify([]), 200

    sessions: list[dict] = []

    for c in CleaningRecord.query.options(joinedload(CleaningRecord.room)).filter_by(cleaner_id=worker_id, end_time=None).all():
        room = c.room
        sessions.append({
            'type': 'cleaning',
            'record_id': c.id,
            'start_time': c.start_time.isoformat(),
            'subject': f'Hab. {room.number}' if room else 'Habitación',
            'subject_sub': room.description or '' if room else '',
        })

    care_records = CareRecord.query.options(
        joinedload(CareRecord.resident), joinedload(CareRecord.care_types),
        joinedload(CareRecord.care_type),
    ).filter_by(worker_id=worker_id, end_time=None).all()

    # Group by start_time (truncated to second) to detect group care sessions
    by_start: dict[str | None, list] = defaultdict(list)
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

    # Flag stale sessions that exceed the configured threshold
    max_minutes = int(AppSetting.get('session_max_minutes', '120'))
    now = datetime.now()
    for sess in sessions:
        elapsed_min = (now - datetime.fromisoformat(sess['start_time'])).total_seconds() / 60
        if elapsed_min >= max_minutes:
            sess['stale'] = True
            sess['elapsed_minutes'] = round(elapsed_min)

    return jsonify(sessions), 200


@bp.route('/api/rooms')
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


@bp.route('/api/residents')
@jwt_required()
def api_residents():
    worker_id = _current_worker_id()

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
        worker_obj = db.session.get(Cleaner, worker_id)
        if worker_obj and worker_obj.groups:
            worker_group_ids = [g.id for g in worker_obj.groups]

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


@bp.route('/api/resident/<int:resident_id>/info')
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
        'allergies': r.allergies or '',
        'dependency_level': r.dependency_level or '',
    }), 200


@bp.route('/api/worker/active-session')
@jwt_required()
def worker_active_session():
    worker_id = _current_worker_id()
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


@bp.route('/api/worker/today')
@jwt_required()
def worker_today():
    worker_id = _current_worker_id()
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


@bp.route('/api/worker/my-groups')
@jwt_required()
def worker_my_groups():
    worker_id = _current_worker_id()
    if not worker_id:
        return jsonify({'groups': []}), 200

    worker_obj = Cleaner.query.options(
        subqueryload(Cleaner.groups).subqueryload(ResidentGroup.residents),
    ).get(worker_id)
    if not worker_obj:
        return jsonify({'groups': []}), 200

    today = datetime.now().date()
    hoy_inicio = datetime.combine(today, datetime.min.time())
    hoy_fin = datetime.combine(today + timedelta(days=1), datetime.min.time())

    # Collect all resident IDs from worker's groups
    group_residents: dict[int, list] = {}
    all_resident_ids: list[int] = []
    for group in worker_obj.groups:
        active_residents = sorted([r for r in group.residents if r.active], key=lambda r: r.name)
        group_residents[group.id] = active_residents
        all_resident_ids.extend(r.id for r in active_residents)

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
    for group in worker_obj.groups:
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


@bp.route('/api/nfc/scan', methods=['POST'])
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
        _, resident = _resolve_nfc_code(nfc_code)
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
        room, resident = _resolve_nfc_code(nfc_code)
        if room and resident:
            # If there's an active session for this code, auto-detect mode instead of asking
            active_cleaning = CleaningRecord.query.filter_by(
                cleaner_id=worker_id, room_id=room.id, end_time=None
            ).first() if room else None
            active_care = CareRecord.query.filter_by(
                worker_id=worker_id, resident_id=resident.id, end_time=None
            ).first() if resident else None
            if active_cleaning:
                mode = 'cleaning'
            elif active_care:
                mode = 'care'
            else:
                return jsonify({
                    'action': 'select_mode',
                    'room': {'number': room.number, 'description': room.description or ''},
                    'resident': {'id': resident.id, 'name': resident.name, 'room_number': resident.room_number or ''},
                }), 200
        if not mode and room:
            mode = 'cleaning'
        elif not mode and resident:
            mode = 'care'
        elif not mode:
            return jsonify({'error': f'Código NFC "{nfc_code}" no reconocido', 'code': 'NFC_NOT_FOUND'}), 404

    if mode == 'cleaning':
        room, _ = _resolve_nfc_code(nfc_code)
        if not room:
            return jsonify({'error': f'Habitación "{nfc_code}" no encontrada', 'code': 'ROOM_NOT_FOUND'}), 404

        active_this = CleaningRecord.query.filter_by(
            cleaner_id=worker_id, room_id=room.id, end_time=None
        ).first()
        if active_this:
            active_this.end_time = now
            ok, err = _safe_commit()
            if not ok:
                return jsonify({'error': err}), 500
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
        if _check_single_session_conflict(worker_id):
            return jsonify({'error': 'Ya tienes una sesión activa. Finalízala antes de iniciar otra.'}), 409

        record = CleaningRecord(cleaner_id=worker_id, room_id=room.id, start_time=now)
        db.session.add(record)
        ok, err = _safe_commit()
        if not ok:
            return jsonify({'error': err}), 500
        return jsonify({
            'action': 'started',
            'type': 'cleaning',
            'record_id': record.id,
            'subject': f'Hab. {room.number}',
            'subject_sub': room.description or '',
            'start_time': now.isoformat(),
        }), 200

    if mode == 'care':
        _, resident = _resolve_nfc_code(nfc_code)
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
        if _check_single_session_conflict(worker_id):
            return jsonify({'error': 'Ya tienes una sesión activa. Finalízala antes de iniciar otra.'}), 409

        # Start session immediately without care type
        record = CareRecord(
            worker_id=worker_id,
            resident_id=resident.id,
            start_time=now,
        )
        db.session.add(record)
        ok, err = _safe_commit()
        if not ok:
            return jsonify({'error': err}), 500
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


@bp.route('/api/nfc/end-session', methods=['POST'])
@jwt_required()
def end_session():
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403

    nfc_only = AppSetting.get('nfc_only', 'true') == 'true'
    if nfc_only:
        return jsonify({'error': 'Modo Solo NFC: escanea la etiqueta NFC para finalizar la sesión'}), 403

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
        ok, err = _safe_commit()
        if not ok:
            return jsonify({'error': err}), 500
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


@bp.route('/api/nfc/start-group-care', methods=['POST'])
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
        # Los residentes dados de baja no forman parte de ningun grupo
        if not resident or not resident.active:
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

    if not records_out:
        db.session.rollback()
        return jsonify({'error': 'Ningun residente disponible'}), 400

    ok, err = _safe_commit()
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({
        'action': 'group_started',
        'group_key': now.replace(microsecond=0).isoformat(),
        'records': records_out,
    }), 200


@bp.route('/api/nfc/finalize-group-care', methods=['POST'])
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

    notes_with_names = []
    for mapping in record_mapping:
        rec = records_by_id.get(mapping['record_id'])
        if not rec:
            continue
        rec.end_time = now
        notes = mapping.get('notes', '').strip()
        if notes:
            rec.notes = notes
            resident_name = rec.resident.name if rec.resident else 'Residente'
            notes_with_names.append((resident_name, notes, rec.resident_id))
        for ct_id in mapping.get('care_type_ids', []):
            ct = db.session.get(CareType, ct_id)
            if ct:
                rec.care_types.append(ct)
                all_type_names.add(ct.name)
        if rec.resident:
            names.append(rec.resident.name)

    # Generate notifications for notes in group care
    if notes_with_names:
        worker = db.session.get(Cleaner, worker_id)
        worker_name = worker.name if worker else 'Trabajador'
        for resident_name, note_text, resident_id in notes_with_names:
            db.session.add(Notification(
                type='worker_note',
                title=f"{worker_name} ha anotado en atención con {resident_name}",
                message=note_text, severity='info',
                worker_id=worker_id, resident_id=resident_id,
                link='/registros-atencion',
            ))

    ok, err = _safe_commit()
    if not ok:
        return jsonify({'error': err}), 500

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


@bp.route('/api/nfc/finalize-care', methods=['POST'])
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
    worker_notes = data.get('notes', '').strip()
    if worker_notes:
        record.notes = worker_notes
    for ct_id in care_type_ids:
        ct = db.session.get(CareType, ct_id)
        if ct:
            record.care_types.append(ct)

    # Validate: if selected care types have vital sign fields, values are required
    provided_vst_ids = {vs.get('vital_sign_type_id') for vs in vital_signs if vs.get('value') not in (None, '')}
    for ct_id in care_type_ids:
        ct = db.session.get(CareType, ct_id)
        if ct:
            for vst in ct.vital_sign_types:
                if vst.active and vst.id not in provided_vst_ids:
                    return jsonify({'error': f'Falta el valor de {vst.name}'}), 400

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
        db.session.add(VitalSignReading(care_record_id=record.id, vital_sign_type_id=vst_id, value=val))
        # Check weight loss if this is a weight reading
        if 'peso' in vst.name.lower():
            from .assessments import check_weight_loss_from_vitals
            check_weight_loss_from_vitals(record.resident_id, val)

    # Generate notification if worker added notes (with AI classification)
    if worker_notes:
        worker = db.session.get(Cleaner, worker_id)
        resident_name = record.resident.name if record.resident else 'Residente'
        worker_name = worker.name if worker else 'Trabajador'
        note_severity = 'info'
        note_prefix = ''
        # Quick AI classification (async-safe, non-blocking on failure)
        try:
            api_key = app.config.get('ANTHROPIC_API_KEY')
            if api_key:
                from anthropic import Anthropic
                client = Anthropic(api_key=api_key)
                cls_resp = client.messages.create(
                    model='claude-haiku-4-5-20251001', max_tokens=50,
                    system='Clasifica esta nota de un trabajador de residencia. Responde SOLO con una palabra: URGENTE, CLINICA, CONDUCTUAL, LOGISTICA o NORMAL',
                    messages=[{'role': 'user', 'content': worker_notes}],
                )
                category = ''.join(b.text for b in cls_resp.content if hasattr(b, 'text')).strip().upper()
                if 'URGENTE' in category:
                    note_severity = 'warning'
                    note_prefix = '⚠ URGENTE: '
                elif 'CLINICA' in category:
                    note_prefix = '[Clinica] '
                elif 'CONDUCTUAL' in category:
                    note_prefix = '[Conductual] '
                elif 'LOGISTICA' in category:
                    note_prefix = '[Logistica] '
        except Exception:
            pass
        notif_title = f"{note_prefix}{worker_name} ha anotado en atencion con {resident_name}"
        db.session.add(Notification(
            type='worker_note', title=notif_title,
            message=worker_notes, severity=note_severity,
            worker_id=worker_id,
            resident_id=record.resident_id,
            link='/registros-atencion',
        ))

    ok, err = _safe_commit()
    if not ok:
        return jsonify({'error': err}), 500

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
        'resident_id': record.resident_id,
        'subject': record.resident.name if record.resident else 'Residente',
        'subject_sub': type_names,
        'duration': record.calculate_duration(),
        'duration_display': _format_duration(record.start_time, record.end_time),
    }
    if vital_alerts:
        resp['vital_alerts'] = vital_alerts
    return jsonify(resp), 200


@bp.route('/api/nfc/finalize-cleaning', methods=['POST'])
@jwt_required()
def finalize_cleaning():
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
    worker_notes = data.get('notes', '').strip()
    if worker_notes:
        record.notes = worker_notes
        worker = db.session.get(Cleaner, worker_id)
        room_desc = f'Hab. {record.room.number}' if record.room else 'Habitacion'
        worker_name = worker.name if worker else 'Trabajador'
        note_severity = 'info'
        note_prefix = ''
        try:
            api_key = app.config.get('ANTHROPIC_API_KEY')
            if api_key:
                from anthropic import Anthropic
                client = Anthropic(api_key=api_key)
                cls_resp = client.messages.create(
                    model='claude-haiku-4-5-20251001', max_tokens=50,
                    system='Clasifica esta nota de un trabajador de residencia. Responde SOLO con una palabra: URGENTE, CLINICA, CONDUCTUAL, LOGISTICA o NORMAL',
                    messages=[{'role': 'user', 'content': worker_notes}],
                )
                category = ''.join(b.text for b in cls_resp.content if hasattr(b, 'text')).strip().upper()
                if 'URGENTE' in category:
                    note_severity = 'warning'
                    note_prefix = '⚠ URGENTE: '
                elif 'LOGISTICA' in category:
                    note_prefix = '[Logistica] '
        except Exception:
            pass
        db.session.add(Notification(
            type='worker_note', title=f"{note_prefix}{worker_name} ha anotado en limpieza de {room_desc}",
            message=worker_notes, severity=note_severity,
            worker_id=worker_id,
            link='/admin/cleaning-records',
        ))
    ok, err = _safe_commit()
    if not ok:
        return jsonify({'error': err}), 500
    room = record.room
    return jsonify({
        'action': 'ended',
        'subject': f'Hab. {room.number}' if room else 'Habitación',
        'subject_sub': room.description or '' if room else '',
        'duration': record.calculate_duration(),
        'duration_display': _format_duration(record.start_time, record.end_time),
    }), 200


@bp.route('/api/nfc/cancel-session', methods=['POST'])
@jwt_required()
def cancel_session():
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403

    nfc_only = AppSetting.get('nfc_only', 'true') == 'true'
    if nfc_only:
        return jsonify({'error': 'Modo Solo NFC: no se pueden cancelar sesiones manualmente'}), 403

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

    ok, err = _safe_commit()
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'message': 'Sesión cancelada'}), 200


# ══════════════════════════════════════════════════════════════════════════════
#  UPLOADS — Servir fitxers
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename: str):
    upload_dir = os.path.normpath(app.config['UPLOAD_FOLDER'])
    safe_path = os.path.normpath(os.path.join(upload_dir, filename))
    if not safe_path.startswith(upload_dir):
        abort(403)
    return send_from_directory(upload_dir, filename)


@bp.route('/api/uploads/<path:filename>')
@jwt_required()
def api_serve_upload(filename: str):
    upload_dir = os.path.normpath(app.config['UPLOAD_FOLDER'])
    safe_path = os.path.normpath(os.path.join(upload_dir, filename))
    if not safe_path.startswith(upload_dir):
        abort(403)
    return send_from_directory(upload_dir, filename)


# ══════════════════════════════════════════════════════════════════════════════
#  IDENTITAT — Selfie d'alta
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/api/worker/identity-status')
@jwt_required()
def worker_identity_status():
    worker_id = _current_worker_id()
    if not worker_id:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
    cleaner = db.session.get(Cleaner, worker_id)
    if not cleaner:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
    return jsonify({'verified': cleaner.identity_verified})


@bp.route('/api/worker/enroll-selfie', methods=['POST'])
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


@bp.route('/api/worker/verify-selfie', methods=['POST'])
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


# ── ADMIN SETTINGS ──────────────────────────────────────────────────────────

@bp.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    if request.method == 'POST':
        AppSetting.set('allow_group_care', 'true' if request.form.get('allow_group_care') else 'false')
        AppSetting.set('nfc_only', 'true' if request.form.get('nfc_only') else 'false')
        AppSetting.set('single_session', 'true' if request.form.get('single_session') else 'false')
        AppSetting.set('allow_worker_incidents', 'true' if request.form.get('allow_worker_incidents') else 'false')
        AppSetting.set('allow_worker_activities', 'true' if request.form.get('allow_worker_activities') else 'false')
        session_max = request.form.get('session_max_minutes', '120')
        try:
            session_max = str(max(15, min(1440, int(session_max))))
        except (ValueError, TypeError):
            session_max = '120'
        AppSetting.set('session_max_minutes', session_max)
        hidden_logout = request.form.get('hidden_logout_minutes', '30')
        try:
            hidden_logout = str(max(0, min(1440, int(hidden_logout))))
        except (ValueError, TypeError):
            hidden_logout = '30'
        AppSetting.set('hidden_logout_minutes', hidden_logout)
        flash('Configuración guardada.', 'success')
        return redirect(url_for('nfc.admin_settings'))
    return render_template('admin_settings.html',
        allow_group_care=AppSetting.get('allow_group_care', 'true') == 'true',
        nfc_only=AppSetting.get('nfc_only', 'true') == 'true',
        single_session=AppSetting.get('single_session', 'false') == 'true',
        allow_worker_incidents=AppSetting.get('allow_worker_incidents', 'false') == 'true',
        allow_worker_activities=AppSetting.get('allow_worker_activities', 'false') == 'true',
        session_max_minutes=int(AppSetting.get('session_max_minutes', '120')),
        hidden_logout_minutes=int(AppSetting.get('hidden_logout_minutes', '30')),
    )


@bp.route('/api/config')
@jwt_required()
def api_config():
    return jsonify({
        'allow_group_care': AppSetting.get('allow_group_care', 'true') == 'true',
        'nfc_only': AppSetting.get('nfc_only', 'true') == 'true',
        'single_session': AppSetting.get('single_session', 'false') == 'true',
        'allow_worker_incidents': AppSetting.get('allow_worker_incidents', 'false') == 'true',
        'allow_worker_activities': AppSetting.get('allow_worker_activities', 'false') == 'true',
        'session_max_minutes': int(AppSetting.get('session_max_minutes', '120')),
        'hidden_logout_minutes': int(AppSetting.get('hidden_logout_minutes', '30')),
    }), 200


# ── Mood tracking ──────────────────────────────────────────────────────────────

@bp.route('/api/mood', methods=['POST'])
@jwt_required()
def api_record_mood():
    """Record a mood/behavior check-in for a resident."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autorizado'}), 403

    data = request.get_json() or {}
    resident_id = data.get('resident_id')
    mood_score = data.get('mood_score')

    if not resident_id or not mood_score:
        return jsonify({'error': 'Faltan datos'}), 400
    if mood_score not in (1, 2, 3, 4, 5):
        return jsonify({'error': 'Puntuación inválida (1-5)'}), 400

    import json
    record = MoodRecord(
        resident_id=resident_id,
        worker_id=worker.id,
        mood_score=mood_score,
        behavior_flags=json.dumps(data.get('behavior_flags', [])),
        notes=data.get('notes', '').strip() or None,
    )
    db.session.add(record)
    db.session.commit()
    return jsonify({'ok': True, 'id': record.id})


@bp.route('/api/resident/<int:resident_id>/mood-history')
@jwt_required()
def api_mood_history(resident_id):
    """Get last 30 days of mood records for a resident."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=30)
    records = MoodRecord.query.filter(
        MoodRecord.resident_id == resident_id,
        MoodRecord.recorded_at >= cutoff,
    ).order_by(MoodRecord.recorded_at).all()

    import json
    return jsonify({'records': [{
        'id': r.id,
        'mood_score': r.mood_score,
        'behavior_flags': json.loads(r.behavior_flags) if r.behavior_flags else [],
        'notes': r.notes,
        'recorded_at': r.recorded_at.strftime('%d/%m %H:%M'),
        'worker': r.worker.name if r.worker else '?',
    } for r in records]})


# ── Meal/intake tracking ──────────────────────────────────────────────────────

@bp.route('/api/meal', methods=['POST'])
@jwt_required()
def api_record_meal():
    """Record a meal/fluid intake for a resident."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autorizado'}), 403

    data = request.get_json() or {}
    resident_id = data.get('resident_id')
    meal_type = data.get('meal_type')  # breakfast, lunch, snack, dinner
    intake_pct = data.get('intake_pct')

    if not resident_id or not meal_type or intake_pct is None:
        return jsonify({'error': 'Faltan datos'}), 400

    from datetime import date as _date
    today = _date.today()

    # Upsert: update if exists for same resident/date/meal
    existing = MealRecord.query.filter_by(
        resident_id=resident_id, date=today, meal_type=meal_type).first()
    if existing:
        existing.intake_pct = intake_pct
        existing.fluid_ml = data.get('fluid_ml')
        existing.texture = data.get('texture')
        existing.notes = data.get('notes', '').strip() or None
        existing.recorded_by = worker.id
        existing.recorded_at = datetime.now()
    else:
        record = MealRecord(
            resident_id=resident_id, date=today,
            meal_type=meal_type, intake_pct=intake_pct,
            fluid_ml=data.get('fluid_ml'),
            texture=data.get('texture'),
            notes=data.get('notes', '').strip() or None,
            recorded_by=worker.id,
        )
        db.session.add(record)

    # Alert if low intake
    if intake_pct < 50:
        resident = db.session.get(Resident, resident_id)
        r_name = resident.name if resident else 'Resident'
        meal_labels = {'breakfast': 'Desayuno', 'lunch': 'Almuerzo', 'snack': 'Merienda', 'dinner': 'Cena'}
        db.session.add(Notification(
            type='vital_alert',
            title=f'Baja ingesta: {r_name} — {meal_labels.get(meal_type, meal_type)} {intake_pct}%',
            severity='warning', resident_id=resident_id,
            link=f'/admin/resident/{resident_id}',
        ))

    db.session.commit()
    return jsonify({'ok': True})


# ── AI Expand Note ─────────────────────────────────────────────────────────
@bp.route('/api/nfc/expand-note', methods=['POST'])
@limiter.limit("10/minute")
@jwt_required()
def expand_note():
    """Expand a terse worker note into professional clinical language."""
    data = request.get_json(silent=True) or {}
    note = (data.get('note') or '').strip()
    if not note or len(note) < 3:
        return jsonify({'error': 'Nota demasiado corta'}), 400

    context = data.get('context', '')  # e.g. 'care', 'cleaning', 'mood', 'meal'

    from .assessments import _call_claude
    system = (
        'Eres un asistente de documentación clínica en una residencia de ancianos. '
        'Tu tarea es expandir notas breves de las cuidadoras en observaciones clínicas profesionales. '
        'Reglas: '
        '- Mantén el significado original, no inventes información. '
        '- Usa lenguaje profesional pero claro, en español. '
        '- Si la nota original es sobre limpieza, adapta el tono (no clínico). '
        '- Genera solo el texto expandido, sin encabezados ni explicaciones. '
        '- Máximo 2-3 frases.'
    )
    if context:
        system += f'\nContexto: esta nota es de tipo "{context}".'

    expanded = _call_claude(system, f'Nota original: "{note}"')
    return jsonify({'expanded': expanded})


# ── Quick Phrases ──────────────────────────────────────────────────────────
_DEFAULT_QUICK_PHRASES = {
    'care': [
        'Sin incidencias', 'Residente tranquilo/a', 'Buen estado general',
        'Inquieto/a, se ha calmado', 'Negativa parcial a la atención',
        'Colaborador/a durante la atención', 'Se queja de dolor',
        'Piel íntegra, sin lesiones',
    ],
    'cleaning': [
        'Sin incidencias', 'Habitación en buen estado',
        'Falta producto de limpieza', 'Grifo gotea',
        'Bombilla fundida', 'Mal olor persistente',
    ],
    'mood': [
        'Sin observaciones', 'Tranquilo/a y comunicativo/a',
        'Apático/a, sin ganas de hablar', 'Llora sin motivo aparente',
        'Agitado/a, cuesta calmarlo/a', 'Contento/a, participa en actividades',
    ],
    'meal': [
        'Ha comido bien', 'Apetito normal', 'Ha rechazado la comida',
        'Solo ha tomado líquidos', 'Dificultad para tragar',
        'Ha comido con ayuda', 'Buena ingesta de líquidos',
    ],
}


@bp.route('/api/nfc/quick-phrases')
@jwt_required()
def get_quick_phrases():
    """Return quick phrases per context, from AppSetting or defaults."""
    stored = AppSetting.get('quick_phrases', None)
    if stored:
        try:
            phrases = _json.loads(stored)
        except (ValueError, TypeError):
            phrases = _DEFAULT_QUICK_PHRASES
    else:
        phrases = _DEFAULT_QUICK_PHRASES
    return jsonify(phrases)


# ── Ambient Voice Scribe ───────────────────────────────────────────────────
@bp.route('/api/nfc/scribe', methods=['POST'])
@limiter.limit("5/minute")
@jwt_required()
def voice_scribe():
    """Transcribe audio recording from a care session and generate clinical notes."""
    audio_file = request.files.get('audio')
    if not audio_file:
        return jsonify({'error': 'No se recibió audio'}), 400

    resident_name = request.form.get('resident_name', '')
    care_duration = request.form.get('duration_minutes', '')

    # Step 1: Transcribe with Whisper
    openai_key = app.config.get('OPENAI_API_KEY')
    if not openai_key:
        return jsonify({'error': 'Transcripción no configurada'}), 503

    import requests as req
    try:
        resp = req.post(
            'https://api.openai.com/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {openai_key}'},
            files={'file': (audio_file.filename or 'audio.webm', audio_file.stream,
                            audio_file.content_type or 'audio/webm')},
            data={'model': 'whisper-1', 'language': 'es'},
            timeout=60,
        )
        if resp.status_code != 200:
            return jsonify({'error': 'Error en la transcripción'}), 500
        transcript = resp.json().get('text', '').strip()
    except Exception:
        return jsonify({'error': 'Error en la transcripción'}), 500

    if not transcript or len(transcript) < 5:
        return jsonify({'transcript': transcript, 'notes': '', 'empty': True})

    # Step 2: Generate clinical notes from transcript
    from .assessments import _call_claude
    system = (
        'Eres un asistente de documentación clínica en la residencia de ancianos La Vila Gran. '
        'Se te proporciona la transcripción de una conversación grabada durante una atención a un residente. '
        'Tu tarea es extraer y redactar las observaciones clínicas relevantes en formato profesional. '
        'Reglas: '
        '- Ignora conversaciones triviales, saludos, o comentarios no relevantes. '
        '- Extrae solo información clínicamente relevante: estado del residente, quejas, observaciones, acciones realizadas. '
        '- Si no hay información clínica relevante, responde con "Sin observaciones clínicas relevantes." '
        '- Escribe en español, máximo 3-4 frases concisas. '
        '- No inventes información que no esté en la transcripción. '
        '- No incluyas encabezados ni explicaciones, solo el texto de las notas.'
    )
    context = f'Residente: {resident_name}\n' if resident_name else ''
    if care_duration:
        context += f'Duración de la atención: {care_duration} minutos\n'
    context += f'Transcripción:\n"{transcript}"'

    notes = _call_claude(system, context)
    return jsonify({'transcript': transcript, 'notes': notes})
