"""Residents, groups, fichajes, and care record view endpoints."""
from __future__ import annotations
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, send_file, abort
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename as _secure_filename
from sqlalchemy.orm import joinedload, subqueryload
from sqlalchemy.exc import IntegrityError
import pandas as pd
from io import BytesIO
import os

from .. import app, db
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..models import (
    Cleaner, Resident, ResidentGroup, CareRecord, CareType,
    CleaningRecord, Room, ResidentDocument,
    VitalSignType, VitalSignReading, MoodRecord,
    WoundRecord, WoundUpdate, Notification,
)
from .assessments import get_resident_assessment_data
from ..models import MedicationPrescription, MedicationAdministration
from ..utils import (
    admin_required, _format_duration, _allowed_file, _safe_commit,
    ALLOWED_IMAGE_EXTENSIONS, ALLOWED_DOC_EXTENSIONS,
    _open_image_oriented, log_audit, _safe_flush,
)

bp = Blueprint('residents', __name__)


# ── HELPERS ──────────────────────────────────────────────────────────────────

def _save_resident_photo(file_storage, resident_id: int) -> str:
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f'res_{resident_id}_{ts}.jpg'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'residents')
    os.makedirs(folder, exist_ok=True)
    file_storage.seek(0)
    try:
        img = _open_image_oriented(file_storage)
        img = img.convert('RGB')
        img.thumbnail((800, 800))
        img.save(os.path.join(folder, filename), 'JPEG', quality=85, optimize=True)
    except (OSError, IOError) as e:
        app.logger.error('Error saving resident photo: %s', e)
        raise ValueError('No se pudo guardar la foto. Formato no válido o disco lleno.')
    return f'residents/{filename}'


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


# ── ADMIN – RESIDENTES ───────────────────────────────────────────────────────

@bp.route('/manage-residents')
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


@bp.route('/residents/add_edit', methods=['POST'])
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
        return redirect(url_for('residents.manage_residents'))

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
                r.diagnoses = request.form.get('diagnoses', '').strip() or None
                r.allergies = request.form.get('allergies', '').strip() or None
                r.current_medication = request.form.get('current_medication', '').strip() or None
                r.blood_type = request.form.get('blood_type', '').strip() or None
                r.dependency_level = request.form.get('dependency_level', '').strip() or None
                r.emergency_contact_name = request.form.get('emergency_contact_name', '').strip() or None
                r.emergency_contact_phone = request.form.get('emergency_contact_phone', '').strip() or None
                r.emergency_contact_relation = request.form.get('emergency_contact_relation', '').strip() or None
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
                log_audit('update', 'resident', r.id, {'nombre': r.name})
                ok, _ = _safe_commit()
                if not ok:
                    flash('El código NFC ya está en uso por otro residente.', 'error')
                    return redirect(url_for('residents.manage_residents'))
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
                diagnoses=request.form.get('diagnoses', '').strip() or None,
                allergies=request.form.get('allergies', '').strip() or None,
                current_medication=request.form.get('current_medication', '').strip() or None,
                blood_type=request.form.get('blood_type', '').strip() or None,
                dependency_level=request.form.get('dependency_level', '').strip() or None,
                emergency_contact_name=request.form.get('emergency_contact_name', '').strip() or None,
                emergency_contact_phone=request.form.get('emergency_contact_phone', '').strip() or None,
                emergency_contact_relation=request.form.get('emergency_contact_relation', '').strip() or None,
            )
            db.session.add(r)
            db.session.flush()  # obtener r.id para el nombre del archivo
            photo_file = request.files.get('photo')
            if photo_file and photo_file.filename and _allowed_file(photo_file.filename, ALLOWED_IMAGE_EXTENSIONS):
                r.photo_path = _save_resident_photo(photo_file, r.id)
            log_audit('create', 'resident', r.id, {'nombre': r.name})
            ok, _ = _safe_commit()
            if not ok:
                flash('El código NFC ya está en uso por otro residente.', 'error')
                return redirect(url_for('residents.manage_residents'))
            flash('Residente añadido correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('El código NFC ya está en uso por otro residente.', 'error')

    return redirect(url_for('residents.manage_residents'))


@bp.route('/residents/<int:resident_id>/rotate-photo', methods=['POST'])
@admin_required
def rotate_resident_photo(resident_id: int):
    """Gira 90 grados la foto ya guardada de un residente.

    Las fotos subidas antes de aplicar la orientacion EXIF perdieron el bloque
    EXIF al reprocesarse, asi que no se pueden enderezar automaticamente: el
    administrador las corrige a mano desde la ficha.
    """
    r = db.session.get(Resident, resident_id)
    if not r or not r.photo_path:
        return jsonify({'error': 'El residente no tiene foto.'}), 404

    payload = request.get_json(silent=True) or {}
    direction = (request.form.get('direction') or payload.get('direction') or 'cw').strip()
    if direction not in ('cw', 'ccw'):
        return jsonify({'error': 'Sentido de giro no valido.'}), 400

    upload_dir = os.path.normpath(app.config['UPLOAD_FOLDER'])
    filepath = os.path.normpath(os.path.join(upload_dir, r.photo_path))
    if not filepath.startswith(upload_dir) or not os.path.exists(filepath):
        return jsonify({'error': 'No se ha encontrado el fichero de la foto.'}), 404

    try:
        from PIL import Image
        img = Image.open(filepath)
        img = img.rotate(-90 if direction == 'cw' else 90, expand=True)
        img.convert('RGB').save(filepath, 'JPEG', quality=85, optimize=True)
    except (OSError, IOError) as e:
        app.logger.error('Error al girar la foto del residente: %s', e)
        return jsonify({'error': 'No se ha podido girar la foto.'}), 500

    log_audit('rotate_photo', 'resident', r.id, {'direction': direction})
    ok, error = _safe_commit('Error al registrar el giro de la foto')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'photo_url': url_for('nfc.serve_upload', filename=r.photo_path)})


@bp.route('/api/resident/<int:resident_id>/update', methods=['POST'])
@admin_required
def update_resident_inline(resident_id: int):
    """Update resident fields via AJAX from detail page."""
    r = db.session.get(Resident, resident_id)
    if not r:
        return jsonify({'error': 'No encontrado'}), 404

    data = request.get_json()
    if 'name' in data:
        r.name = data['name'].strip()
    if 'room_number' in data:
        r.room_number = data['room_number'].strip() or None
    if 'nfc_code' in data:
        r.nfc_code = data['nfc_code'].strip()
    if 'group_id' in data:
        r.group_id = int(data['group_id']) if data['group_id'] else None
    if 'notes' in data:
        r.notes = data['notes'].strip() or None
    if 'relevant_info' in data:
        r.relevant_info = data['relevant_info'].strip() or None
    if 'diagnoses' in data:
        r.diagnoses = data['diagnoses'].strip() or None
    if 'allergies' in data:
        r.allergies = data['allergies'].strip() or None
    if 'current_medication' in data:
        r.current_medication = data['current_medication'].strip() or None
    if 'blood_type' in data:
        r.blood_type = data['blood_type'].strip() or None
    if 'dependency_level' in data:
        r.dependency_level = data['dependency_level'].strip() or None
    if 'emergency_contact_name' in data:
        r.emergency_contact_name = data['emergency_contact_name'].strip() or None
    if 'emergency_contact_phone' in data:
        r.emergency_contact_phone = data['emergency_contact_phone'].strip() or None
    if 'emergency_contact_relation' in data:
        r.emergency_contact_relation = data['emergency_contact_relation'].strip() or None

    ok, _ = _safe_flush()
    if not ok:
        return jsonify({'error': 'El código NFC ya está en uso'}), 400
    log_audit('update', 'resident', resident_id,
              {'campos': sorted(k for k in data if k != 'photo')})
    ok, error = _safe_commit('Error al actualizar el residente')
    if not ok:
        return jsonify({'error': error}), 500

    return jsonify({'ok': True})


@bp.route('/residents/delete/<int:id>', methods=['POST'])
@admin_required
def delete_resident(id: int):
    r = db.session.get(Resident, id)
    if r is None:
        abort(404)
    try:
        log_audit('delete', 'resident', id, {'nombre': r.name})
        db.session.delete(r)
        ok, _ = _safe_commit()
        if not ok:
            flash('No se puede eliminar porque tiene registros de atención asociados.', 'error')
            return redirect(url_for('residents.manage_residents'))
        flash('Residente eliminado correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque tiene registros de atención asociados.', 'error')
    return redirect(url_for('residents.manage_residents'))


@bp.route('/residents/update-group', methods=['POST'])
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
    if group_id and not r.active:
        return jsonify({'error': 'Un residente dado de baja no puede asignarse a un grupo'}), 400
    r.group_id = int(group_id) if group_id else None
    ok, error = _safe_commit('Error al cambiar el grupo del residente')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True}), 200


@bp.route('/residents/update-active', methods=['POST'])
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
    log_audit('update', 'resident', r.id, {'activo': r.active})
    ok, error = _safe_commit('Error al cambiar el estado del residente')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True}), 200


# ── RESIDENT DOCUMENTS ──────────────────────────────────────────────────────

def _docs_redirect():
    """Vuelve a la pagina desde la que se subio o borro el documento.

    El formulario envia el destino en `next` (la ficha del residente o el
    listado). Se valida como en el login: solo rutas internas.
    """
    target = (request.form.get('next') or '').strip()
    if (target.startswith('/') and not target.startswith('//')
            and ':' not in target):
        return redirect(target)
    return redirect(url_for('residents.manage_residents'))


@bp.route('/residents/<int:resident_id>/documents', methods=['POST'])
@admin_required
def upload_resident_document(resident_id: int):
    r = db.session.get(Resident, resident_id)
    if not r:
        flash('Residente no encontrado.', 'error')
        return redirect(url_for('residents.manage_residents'))

    doc_file = request.files.get('doc_file')
    if not doc_file or not doc_file.filename:
        flash('Selecciona un archivo.', 'error')
        return _docs_redirect()

    if not _allowed_file(doc_file.filename, ALLOWED_DOC_EXTENSIONS | ALLOWED_IMAGE_EXTENSIONS):
        flash('Tipo de archivo no permitido. Usa PDF, TXT, DOC, DOCX o imágenes.', 'error')
        return _docs_redirect()

    doc_type = request.form.get('doc_type', '').strip() or 'Otros'
    description = request.form.get('doc_description', '').strip()

    # Save file
    original = doc_file.filename
    safe_name = _secure_filename(original)
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f'{ts}_{safe_name}'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'resident_docs', f'res_{resident_id}')
    os.makedirs(folder, exist_ok=True)
    try:
        doc_file.save(os.path.join(folder, filename))
    except (OSError, IOError) as e:
        app.logger.error('Error saving document: %s', e)
        flash('Error al guardar el archivo. Inténtalo de nuevo.', 'error')
        return _docs_redirect()
    rel_path = f'resident_docs/res_{resident_id}/{filename}'

    doc = ResidentDocument(
        resident_id=resident_id,
        file_path=rel_path,
        original_filename=original,
        doc_type=doc_type,
        description=description,
    )
    db.session.add(doc)

    log_audit('create', 'resident_document', resident_id,
              {'filename': original, 'doc_type': doc_type})
    ok, error = _safe_commit('Error al guardar el documento')
    if not ok:
        flash(error, 'danger')
        return _docs_redirect()
    flash(f'Documento "{original}" subido correctamente.', 'success')
    return _docs_redirect()


@bp.route('/residents/documents/<int:doc_id>/delete', methods=['POST'])
@admin_required
def delete_resident_document(doc_id: int):
    doc = db.session.get(ResidentDocument, doc_id)
    if not doc:
        flash('Documento no encontrado.', 'error')
        return _docs_redirect()
    # Delete file
    full_path = os.path.join(app.config['UPLOAD_FOLDER'], doc.file_path)
    try:
        if os.path.exists(full_path):
            os.remove(full_path)
    except OSError as e:
        app.logger.warning('Could not delete file %s: %s', full_path, e)
    resident_id, original = doc.resident_id, doc.original_filename
    db.session.delete(doc)

    log_audit('delete', 'resident_document', resident_id, {'filename': original})
    ok, error = _safe_commit('Error al eliminar el documento')
    if not ok:
        flash(error, 'danger')
        return _docs_redirect()
    flash('Documento eliminado.', 'success')
    return _docs_redirect()


# ── ADMIN – GRUPOS DE RESIDENTES ────────────────────────────────────────────

@bp.route('/manage-groups')
@admin_required
def manage_groups():
    groups = ResidentGroup.query.order_by(ResidentGroup.name).all()
    # Los residentes dados de baja (active=False) no cuentan como miembros del grupo
    resident_counts = dict(
        db.session.query(Resident.group_id, db.func.count(Resident.id))
        .filter(Resident.active == True, Resident.group_id.isnot(None))
        .group_by(Resident.group_id)
        .all()
    )
    return render_template('manage_groups.html', groups=groups,
                           resident_counts=resident_counts)


@bp.route('/groups/<int:id>')
@admin_required
def group_detail(id: int):
    group = db.session.get(ResidentGroup, id)
    if group is None:
        abort(404)
    # Solo residentes activos: los dados de baja no aparecen en el grupo
    residents = Resident.query.filter_by(group_id=group.id, active=True).order_by(Resident.name).all()
    available = Resident.query.filter(
        Resident.active == True,
        (Resident.group_id == None) | (Resident.group_id != group.id),
    ).order_by(Resident.name).all()
    return render_template('group_detail.html', group=group, residents=residents, available=available)


@bp.route('/groups/add_edit', methods=['POST'])
@admin_required
def add_edit_group():
    group_id = request.form.get('group_id')
    name = request.form.get('name', '').strip()
    color = request.form.get('color', '#000000').strip()

    if not name:
        flash('El nombre es obligatorio.', 'error')
        return redirect(url_for('residents.manage_groups'))

    try:
        if group_id:
            g = db.session.get(ResidentGroup, int(group_id))
            if g:
                g.name = name
                g.color = color
                log_audit('update', 'resident_group', g.id, {'nombre': name})
                ok, _ = _safe_commit()
                if not ok:
                    flash('Ya existe un grupo con ese nombre.', 'error')
                    return redirect(url_for('residents.manage_groups'))
                flash('Grupo actualizado correctamente.', 'success')
            else:
                flash('Grupo no encontrado.', 'error')
        else:
            db.session.add(ResidentGroup(name=name, color=color))
            log_audit('create', 'resident_group', None, {'nombre': name})
            ok, _ = _safe_commit()
            if not ok:
                flash('Ya existe un grupo con ese nombre.', 'error')
                return redirect(url_for('residents.manage_groups'))
            flash('Grupo añadido correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Ya existe un grupo con ese nombre.', 'error')

    return redirect(url_for('residents.manage_groups'))


@bp.route('/groups/delete/<int:id>', methods=['POST'])
@admin_required
def delete_group(id: int):
    g = db.session.get(ResidentGroup, id)
    if g is None:
        abort(404)
    try:
        log_audit('delete', 'resident_group', id, {'nombre': g.name})
        db.session.delete(g)
        ok, _ = _safe_commit()
        if not ok:
            flash('No se puede eliminar porque tiene residentes o trabajadores asignados.', 'error')
            return redirect(url_for('residents.manage_groups'))
        flash('Grupo eliminado correctamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('No se puede eliminar porque tiene residentes o trabajadores asignados.', 'error')
    return redirect(url_for('residents.manage_groups'))


@bp.route('/groups/<int:id>/assign-residents', methods=['POST'])
@admin_required
def assign_residents_to_group(id: int):
    group = db.session.get(ResidentGroup, id)
    if not group:
        return jsonify({'error': 'Grupo no encontrado'}), 404
    data = request.json or {}
    resident_ids = data.get('resident_ids', [])
    try:
        ids = [int(rid) for rid in resident_ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'Identificadores de residente no validos'}), 400

    # Los residentes inactivos no pueden formar parte de un grupo
    residents = Resident.query.filter(
        Resident.id.in_(ids), Resident.active == True
    ).all()
    for r in residents:
        r.group_id = group.id

    log_audit('update', 'resident_group', group.id,
              {'assigned': [r.id for r in residents]})
    ok, error = _safe_commit('Error al asignar los residentes al grupo')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({
        'ok': True,
        'count': len(residents),
        'skipped': len(set(ids)) - len(residents),
    }), 200


# ── ADMIN – FICHAJES POR TRABAJADOR ─────────────────────────────────────────

@bp.route('/fichajes')
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
            return redirect(url_for('residents.fichajes_trabajador'))

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


@bp.route('/exportar_fichajes')
@admin_required
def exportar_fichajes():
    worker_id = request.args.get('worker_id', '', type=str)
    month = request.args.get('month', '')

    if not worker_id or not month:
        flash('Selecciona un trabajador y un mes.', 'error')
        return redirect(url_for('residents.fichajes_trabajador'))

    try:
        year, mon = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        flash('Formato de mes no válido.', 'error')
        return redirect(url_for('residents.fichajes_trabajador'))

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


# ── ADMIN – REGISTROS DE ATENCIÓN ───────────────────────────────────────────

@bp.route('/registros-atencion')
@admin_required
def registros_atencion():
    worker_id = request.args.get('worker_id', '')
    resident_id = request.args.get('resident_id', '')
    care_type_id = request.args.get('care_type_id', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    estado = request.args.get('estado', '')

    query = CareRecord.query.options(
        joinedload(CareRecord.resident),
        joinedload(CareRecord.worker),
        joinedload(CareRecord.care_type),
        joinedload(CareRecord.care_types),
        joinedload(CareRecord.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
    )

    if estado == 'abiertas':
        query = query.filter(CareRecord.end_time.is_(None))
    elif estado == 'cerradas':
        query = query.filter(CareRecord.end_time.isnot(None))
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
        'estado': estado,
    }

    return render_template(
        'registros_atencion.html',
        records=pagination.items,
        pagination=pagination,
        workers=Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all(),
        residents=Resident.query.order_by(Resident.name).all(),
        care_types=CareType.query.order_by(CareType.name).all(),
        filters=filters,
    )


@bp.route('/exportar_atenciones_excel')
@admin_required
def exportar_atenciones_excel():
    worker_id = request.args.get('worker_id', '')
    resident_id = request.args.get('resident_id', '')
    care_type_id = request.args.get('care_type_id', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    estado = request.args.get('estado', '')

    query = CareRecord.query.options(
        joinedload(CareRecord.resident),
        joinedload(CareRecord.worker),
        joinedload(CareRecord.care_types),
        joinedload(CareRecord.care_type),
        joinedload(CareRecord.vital_sign_readings).joinedload(VitalSignReading.vital_sign_type),
    )
    if estado == 'abiertas':
        query = query.filter(CareRecord.end_time.is_(None))
    elif estado == 'cerradas':
        query = query.filter(CareRecord.end_time.isnot(None))
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


# ── RESIDENT DETAIL ─────────────────────────────────────────────────────────

@bp.route('/admin/resident/<int:resident_id>')
@admin_required
def resident_detail(resident_id: int):
    resident = db.session.get(Resident, resident_id)
    if not resident:
        flash('Residente no encontrado.', 'error')
        return redirect(url_for('residents.manage_residents'))

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

    # Assessment data (Barthel, Norton)
    assess_data = get_resident_assessment_data(resident_id)

    # Groups for edit form
    groups = ResidentGroup.query.order_by(ResidentGroup.name).all()

    # Medication data
    prescriptions = MedicationPrescription.query.filter_by(
        resident_id=resident_id,
    ).order_by(MedicationPrescription.active.desc(), MedicationPrescription.drug_name).all()

    from .medication import ROUTE_LABELS

    return render_template(
        'resident_detail.html',
        resident=resident,
        records=pagination.items,
        pagination=pagination,
        vital_charts=list(vital_charts.values()),
        heatmap_data=heatmap_data,
        prescriptions=prescriptions,
        route_labels=ROUTE_LABELS,
        groups=groups,
        **assess_data,
    )


@bp.route('/admin/resident/<int:resident_id>/export-excel')
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


@bp.route('/admin/care-record/<int:record_id>/delete', methods=['POST'])
@admin_required
def delete_care_record(record_id: int):
    if not current_user.is_admin:
        abort(403)
    record = db.session.get(CareRecord, record_id)
    if not record:
        flash('Registro no encontrado.', 'error')
        return redirect(url_for('residents.registros_atencion'))
    VitalSignReading.query.filter_by(care_record_id=record.id).delete()
    record.care_types.clear()
    log_audit('delete', 'care_record', record.id,
              {'resident_id': record.resident_id})
    db.session.delete(record)
    ok, error = _safe_commit('Error al eliminar el registro de atencion')
    if not ok:
        flash(error, 'danger')
    else:
        flash('Registro de atención eliminado.', 'success')
    return redirect(request.referrer or url_for('residents.registros_atencion'))


@bp.route('/api/resident/<int:resident_id>/mood-history')
@admin_required
def admin_mood_history(resident_id):
    """Get mood records for admin charts."""
    import json
    cutoff = datetime.now() - timedelta(days=30)
    records = MoodRecord.query.filter(
        MoodRecord.resident_id == resident_id,
        MoodRecord.recorded_at >= cutoff,
    ).order_by(MoodRecord.recorded_at).all()

    return jsonify({'records': [{
        'id': r.id,
        'mood_score': r.mood_score,
        'behavior_flags': json.loads(r.behavior_flags) if r.behavior_flags else [],
        'notes': r.notes,
        'recorded_at': r.recorded_at.strftime('%d/%m %H:%M'),
        'worker': r.worker.name if r.worker else '?',
    } for r in records]})


# ── Wound Body Map ────────────────────────────────────────────────────────

WOUND_TYPES = {
    'ulcer': 'Úlcera', 'bruise': 'Hematoma', 'cut': 'Corte/herida',
    'burn': 'Quemadura', 'rash': 'Erupción', 'other': 'Otra',
}

BODY_ZONES = {
    'head': 'Cabeza', 'torso_front': 'Tronco (frente)', 'torso_back': 'Tronco (espalda)',
    'left_arm': 'Brazo izquierdo', 'right_arm': 'Brazo derecho',
    'left_leg': 'Pierna izquierda', 'right_leg': 'Pierna derecha',
    'sacrum': 'Sacro', 'left_heel': 'Talón izquierdo', 'right_heel': 'Talón derecho',
}


@bp.route('/api/resident/<int:resident_id>/wounds')
@login_required
def get_wounds(resident_id: int):
    """Get all wounds for a resident."""
    wounds = WoundRecord.query.filter_by(resident_id=resident_id).order_by(
        WoundRecord.status != 'healed', WoundRecord.created_at.desc()
    ).all()
    return jsonify({'wounds': [{
        'id': w.id,
        'body_zone': w.body_zone,
        'body_zone_label': BODY_ZONES.get(w.body_zone, w.body_zone),
        'body_x': w.body_x,
        'body_y': w.body_y,
        'wound_type': w.wound_type,
        'wound_type_label': WOUND_TYPES.get(w.wound_type, w.wound_type),
        'description': w.description or '',
        'size_cm': w.size_cm or '',
        'severity': w.severity,
        'status': w.status,
        'photo_path': w.photo_path,
        'reported_by': w.reporter.name if w.reporter else '',
        'created_at': w.created_at.strftime('%d/%m/%Y %H:%M'),
        'updated_at': w.updated_at.strftime('%d/%m/%Y %H:%M') if w.updated_at else None,
        'healed_at': w.healed_at.strftime('%d/%m/%Y') if w.healed_at else None,
        'notes': w.notes or '',
        'updates_count': len(w.updates),
    } for w in wounds],
    'wound_types': WOUND_TYPES,
    'body_zones': BODY_ZONES,
    })


@bp.route('/api/resident/<int:resident_id>/wounds', methods=['POST'])
@login_required
def create_wound(resident_id: int):
    """Register a new wound on the body map."""
    data = request.get_json(silent=True) or {}

    wound = WoundRecord(
        resident_id=resident_id,
        body_zone=data.get('body_zone', 'other'),
        body_x=data.get('body_x'),
        body_y=data.get('body_y'),
        wound_type=data.get('wound_type', 'other'),
        description=(data.get('description') or '').strip() or None,
        size_cm=(data.get('size_cm') or '').strip() or None,
        severity=data.get('severity', 'moderate'),
        reported_by=current_user.id,
        notes=(data.get('notes') or '').strip() or None,
    )
    db.session.add(wound)
    ok, error = _safe_flush('Error al registrar la herida')
    if not ok:
        return jsonify({'error': error}), 500

    # Notification
    resident = db.session.get(Resident, resident_id)
    r_name = resident.name if resident else 'Resident'
    type_label = WOUND_TYPES.get(wound.wound_type, wound.wound_type)
    zone_label = BODY_ZONES.get(wound.body_zone, wound.body_zone)
    db.session.add(Notification(
        type='wound_alert',
        title=f'Nueva herida: {r_name} — {type_label} ({zone_label})',
        severity='warning' if wound.severity in ('moderate', 'severe') else 'info',
        resident_id=resident_id,
        link=f'/admin/resident/{resident_id}',
    ))
    log_audit('create', 'wound', wound.id,
              {'resident_id': resident_id, 'severidad': wound.severity})
    ok, error = _safe_commit('Error al registrar la herida')
    if not ok:
        return jsonify({'error': error}), 500

    return jsonify({'ok': True, 'id': wound.id}), 201


@bp.route('/api/wound/<int:wound_id>/update', methods=['POST'])
@login_required
def update_wound(wound_id: int):
    """Add a follow-up update to a wound."""
    wound = db.session.get(WoundRecord, wound_id)
    if not wound:
        return jsonify({'error': 'Herida no encontrada'}), 404

    data = request.get_json(silent=True) or {}
    new_status = data.get('status', wound.status)

    update = WoundUpdate(
        wound_id=wound_id,
        status=new_status,
        size_cm=(data.get('size_cm') or '').strip() or None,
        description=(data.get('description') or '').strip() or None,
        updated_by=current_user.id,
    )
    db.session.add(update)

    wound.status = new_status
    wound.updated_at = datetime.now()
    if new_status == 'healed':
        wound.healed_at = datetime.now()
    if data.get('size_cm'):
        wound.size_cm = data['size_cm'].strip()

    # Alert if worsening
    if new_status == 'worsening':
        resident = db.session.get(Resident, wound.resident_id)
        r_name = resident.name if resident else 'Resident'
        db.session.add(Notification(
            type='wound_alert',
            title=f'Herida empeorando: {r_name} — {WOUND_TYPES.get(wound.wound_type, wound.wound_type)}',
            severity='critical', resident_id=wound.resident_id,
            link=f'/admin/resident/{wound.resident_id}',
        ))

    log_audit('update', 'wound', wound.id,
              {'resident_id': wound.resident_id, 'severidad': wound.severity})
    ok, error = _safe_commit('Error al actualizar la herida')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True})


@bp.route('/api/wound/<int:wound_id>/history')
@login_required
def wound_history(wound_id: int):
    """Get evolution history for a wound."""
    wound = db.session.get(WoundRecord, wound_id)
    if not wound:
        return jsonify({'error': 'No encontrada'}), 404

    updates = WoundUpdate.query.filter_by(wound_id=wound_id).order_by(WoundUpdate.created_at.desc()).all()
    return jsonify({
        'wound': {
            'id': wound.id, 'wound_type': wound.wound_type, 'body_zone': wound.body_zone,
            'status': wound.status, 'created_at': wound.created_at.strftime('%d/%m/%Y'),
        },
        'updates': [{
            'status': u.status,
            'size_cm': u.size_cm or '',
            'description': u.description or '',
            'updated_by': u.updater.name if u.updater else '',
            'created_at': u.created_at.strftime('%d/%m/%Y %H:%M'),
        } for u in updates],
    })


# ── Worker API: Wounds (JWT) ──────────────────────────────────────────────

@bp.route('/api/worker/resident/<int:resident_id>/wounds')
@jwt_required()
def worker_get_wounds(resident_id: int):
    """Get wounds for a resident (worker view)."""
    wounds = WoundRecord.query.filter_by(resident_id=resident_id).filter(
        WoundRecord.status != 'healed'
    ).order_by(WoundRecord.created_at.desc()).all()
    return jsonify({'wounds': [{
        'id': w.id,
        'body_zone': w.body_zone,
        'body_zone_label': BODY_ZONES.get(w.body_zone, w.body_zone),
        'wound_type': w.wound_type,
        'wound_type_label': WOUND_TYPES.get(w.wound_type, w.wound_type),
        'description': w.description or '',
        'size_cm': w.size_cm or '',
        'severity': w.severity,
        'status': w.status,
        'body_x': w.body_x,
        'body_y': w.body_y,
        'created_at': w.created_at.strftime('%d/%m/%Y'),
    } for w in wounds]})


@bp.route('/api/worker/resident/<int:resident_id>/wounds', methods=['POST'])
@jwt_required()
def worker_create_wound(resident_id: int):
    """Register a wound from the worker app."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autorizado'}), 403

    data = request.get_json(silent=True) or {}
    wound = WoundRecord(
        resident_id=resident_id,
        body_zone=data.get('body_zone', 'other'),
        body_x=data.get('body_x'),
        body_y=data.get('body_y'),
        wound_type=data.get('wound_type', 'other'),
        description=(data.get('description') or '').strip() or None,
        size_cm=(data.get('size_cm') or '').strip() or None,
        severity=data.get('severity', 'moderate'),
        reported_by=worker.id,
    )
    db.session.add(wound)
    ok, error = _safe_flush('Error al registrar la herida')
    if not ok:
        return jsonify({'error': error}), 500

    resident = db.session.get(Resident, resident_id)
    r_name = resident.name if resident else 'Residente'
    db.session.add(Notification(
        type='wound_alert',
        title=f'Nueva herida: {r_name} — {WOUND_TYPES.get(wound.wound_type, wound.wound_type)} ({BODY_ZONES.get(wound.body_zone, wound.body_zone)})',
        severity='warning' if wound.severity in ('moderate', 'severe') else 'info',
        resident_id=resident_id,
        link=f'/admin/resident/{resident_id}',
    ))
    log_audit('create', 'wound', wound.id,
              {'resident_id': resident_id, 'severidad': wound.severity,
               'cleaner_id': worker.id})
    ok, error = _safe_commit('Error al registrar la herida')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'id': wound.id}), 201
