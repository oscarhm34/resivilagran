"""Care types, vital signs, and checklist management."""
from __future__ import annotations

from flask import Blueprint, request, jsonify, redirect, url_for, flash, abort, render_template
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from datetime import datetime
import os

from .. import db
from ..models import CareType, VitalSignType, ChecklistItem
from ..utils import (
    admin_required, _allowed_file, ALLOWED_IMAGE_EXTENSIONS, _open_image_oriented,
)

bp = Blueprint('care', __name__)


# ── HELPER ──────────────────────────────────────────────────────────────────

def _save_care_type_icon(file_storage, care_type_id: int) -> str:
    from flask import current_app
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f'ct_{care_type_id}_{ts}.png'
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'care_icons')
    os.makedirs(folder, exist_ok=True)
    file_storage.seek(0)
    img = _open_image_oriented(file_storage)
    img = img.convert('RGBA')
    img.thumbnail((128, 128))
    img.save(os.path.join(folder, filename), 'PNG', optimize=True)
    return f'care_icons/{filename}'


# ── ADMIN – TIPOS DE ATENCIÓN ───────────────────────────────────────────────

@bp.route('/manage-care-types')
@admin_required
def manage_care_types():
    # Show parent types first, then children indented
    parents = CareType.query.filter_by(parent_id=None).order_by(CareType.sort_order, CareType.name).all()
    all_parents = CareType.query.filter_by(parent_id=None).order_by(CareType.name).all()
    # Collect all unique uploaded icons for the icon library
    uploaded_icons = CareType.query.filter(CareType.icon_path.isnot(None)).with_entities(CareType.icon_path).distinct().all()
    uploaded_icons = [row[0] for row in uploaded_icons]
    return render_template('manage_care_types.html', parents=parents, all_parents=all_parents, uploaded_icons=uploaded_icons)


@bp.route('/care-types/add_edit', methods=['POST'])
@admin_required
def add_edit_care_type():
    from flask import current_app
    care_type_id = request.form.get('care_type_id')
    name = request.form.get('name', '').strip()
    icon = request.form.get('icon', '').strip()
    parent_id = request.form.get('parent_id', '').strip()
    sort_order = request.form.get('sort_order', '0').strip()
    selected_icon_path = request.form.get('selected_icon_path', '').strip()

    if not name:
        flash('El nombre es obligatorio.', 'error')
        return redirect(url_for('care.manage_care_types'))
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
                        old = os.path.join(current_app.config['UPLOAD_FOLDER'], ct.icon_path)
                        if os.path.exists(old):
                            os.remove(old)
                    ct.icon_path = _save_care_type_icon(icon_file, ct.id)
                elif selected_icon_path:
                    ct.icon_path = selected_icon_path
                if request.form.get('remove_icon') == '1' and ct.icon_path:
                    old = os.path.join(current_app.config['UPLOAD_FOLDER'], ct.icon_path)
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
    return redirect(url_for('care.manage_care_types'))


@bp.route('/care-types/delete/<int:id>', methods=['POST'])
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
    return redirect(url_for('care.manage_care_types'))


@bp.route('/care-types/toggle-active', methods=['POST'])
@admin_required
def toggle_care_type_active():
    data = request.json or {}
    ct = db.session.get(CareType, int(data.get('id', 0)))
    if not ct:
        return jsonify({'error': 'No encontrado'}), 404
    ct.active = data.get('active', True)
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── ADMIN – VITAL SIGN TYPES ───────────────────────────────────────────────

@bp.route('/care-types/<int:care_type_id>/vital-fields/add_edit', methods=['POST'])
@admin_required
def add_edit_vital_field(care_type_id: int):
    ct = db.session.get(CareType, care_type_id)
    if not ct:
        flash('Tipo de atención no encontrado.', 'error')
        return redirect(url_for('care.manage_care_types'))
    vf_id = request.form.get('vf_id')
    name = request.form.get('vf_name', '').strip()
    unit = request.form.get('vf_unit', '').strip()
    min_val = request.form.get('vf_min', '').strip()
    max_val = request.form.get('vf_max', '').strip()
    input_type = request.form.get('vf_input_type', 'number').strip()
    sort_order = request.form.get('vf_sort_order', '0').strip()
    if not name or not unit:
        flash('Nombre y unidad son obligatorios.', 'error')
        return redirect(url_for('care.manage_care_types'))
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
    return redirect(url_for('care.manage_care_types'))


@bp.route('/care-types/vital-fields/delete/<int:vf_id>', methods=['POST'])
@admin_required
def delete_vital_field(vf_id: int):
    vf = db.session.get(VitalSignType, vf_id)
    if not vf:
        flash('Campo no encontrado.', 'error')
        return redirect(url_for('care.manage_care_types'))
    if vf.readings:
        vf.active = False
        flash(f'Campo "{vf.name}" desactivado (tiene lecturas asociadas).', 'warning')
    else:
        db.session.delete(vf)
        flash(f'Campo "{vf.name}" eliminado.', 'success')
    db.session.commit()
    return redirect(url_for('care.manage_care_types'))


# ── ADMIN – CHECKLIST DE LIMPIEZA ───────────────────────────────────────────

@bp.route('/manage-checklist')
@admin_required
def manage_checklist():
    items = ChecklistItem.query.order_by(ChecklistItem.sort_order, ChecklistItem.id).all()
    return render_template('manage_checklist.html', items=items)


@bp.route('/checklist/add_edit', methods=['POST'])
@admin_required
def add_edit_checklist_item():
    item_id = request.form.get('item_id')
    text = request.form.get('text', '').strip()
    sort_order = request.form.get('sort_order', '0').strip()

    if not text:
        flash('El texto es obligatorio.', 'error')
        return redirect(url_for('care.manage_checklist'))

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
    return redirect(url_for('care.manage_checklist'))


@bp.route('/checklist/delete/<int:id>', methods=['POST'])
@admin_required
def delete_checklist_item(id: int):
    item = db.session.get(ChecklistItem, id)
    if item:
        db.session.delete(item)
        db.session.commit()
        flash('Item eliminado.', 'success')
    return redirect(url_for('care.manage_checklist'))


@bp.route('/checklist/toggle-active', methods=['POST'])
@admin_required
def toggle_checklist_active():
    data = request.json or {}
    item = db.session.get(ChecklistItem, int(data.get('id', 0)))
    if not item:
        return jsonify({'error': 'No encontrado'}), 404
    item.active = data.get('active', True)
    db.session.commit()
    return jsonify({'ok': True}), 200
