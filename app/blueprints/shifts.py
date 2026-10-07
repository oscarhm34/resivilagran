"""Shifts / Cuadrantes blueprint — admin shift management + worker API."""
from __future__ import annotations
import re
from datetime import datetime, timedelta, date, time as dt_time

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, send_file
from flask_login import current_user
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from .. import app, db, limiter
from ..models import (
    Cleaner, ResidentGroup, ShiftType, ShiftAssignment,
    RotationPattern, RotationPatternDay, WorkerShiftConfig,
    AbsenceType, Absence, ShiftCoverageRequirement,
    ShiftPosition, ShiftWeekPublication, ShiftWeekReceipt, Notification,
)
from ..utils import (
    admin_required, _safe_commit, _safe_flush, _parse_hhmm, log_audit,
)

bp = Blueprint('shifts', __name__)

DEFAULT_SHIFT_COLOR = '#0d6efd'
_HEX_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


# ── TURNOS / CUADRANTES ─────────────────────────────────────────────────────

@bp.route('/cuadrantes')
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

    # {(cleaner_id, date_iso): [assignment, ...]}. Una lista y no una sola:
    # desde que se puede doblar un M1 con un T1, un dia lleva mas de un turno y
    # quedarse con el ultimo escondia justo el dia que mas hay que mirar.
    assign_map = {}
    for a in assignments:
        assign_map.setdefault((a.cleaner_id, a.date.isoformat()), []).append(a)
    for filas in assign_map.values():
        filas.sort(key=lambda x: (x.shift_type.start_time if x.shift_type else dt_time(0, 0)))

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
        coverage_reqs={f'{r.shift_type_id}_{r.day_type}': {'min': r.min_workers, 'ideal': r.ideal_workers or 0} for r in ShiftCoverageRequirement.query.all()},
        patterns=RotationPattern.query.filter_by(active=True).order_by(RotationPattern.name).all(),
        groups=groups, group_id=group_id, role_filter=role_filter,
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
    )


@bp.route('/cuadrantes/assign', methods=['POST'])
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
            existing.updated_at = datetime.now()
        else:
            # is_override=True tambien al crear: sin esto, lo que se pone a
            # mano en una celda vacia lo borra la siguiente generacion del mes
            # (scheduler._persist solo respeta los overrides).
            existing = ShiftAssignment(
                cleaner_id=cleaner_id, date=target_date,
                shift_type_id=st.id, source='manual',
                is_override=True, created_by=current_user.id,
            )
            db.session.add(existing)
        ok, err = _safe_commit('No se pudo guardar la asignacion.')
        if not ok:
            return jsonify({'error': err}), 500
        return jsonify({'ok': True, 'short_name': st.short_name, 'color': st.color})
    else:
        # Clear assignment (dia libre)
        if existing:
            db.session.delete(existing)
            ok, err = _safe_commit('No se pudo borrar la asignacion.')
            if not ok:
                return jsonify({'error': err}), 500
        return jsonify({'ok': True, 'short_name': '', 'color': ''})


@bp.route('/cuadrantes/bulk-assign', methods=['POST'])
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
        ok, err = _safe_commit('No se pudo aplicar la accion en bloque.')
        if not ok:
            return jsonify({'error': err}), 500
        return jsonify({'ok': True})

    return jsonify({'error': 'Accion no valida'}), 400


@bp.route('/cuadrantes/clear', methods=['POST'])
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
    ok, err = _safe_commit('No se pudo vaciar el cuadrante.')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True, 'deleted': deleted})


@bp.route('/cuadrantes/export')
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


@bp.route('/cuadrantes/manage-shift-types')
@admin_required
def manage_shift_types():
    shift_types = ShiftType.query.order_by(ShiftType.sort_order).all()
    return render_template('manage_shift_types.html', shift_types=shift_types,
                           etiquetas_fila=ETIQUETAS_FILA)


@bp.route('/shift-types/add_edit', methods=['POST'])
@admin_required
def add_edit_shift_type():
    st_id = request.form.get('shift_type_id', '').strip()
    name = request.form.get('name', '').strip()
    short_name = request.form.get('short_name', '').strip()
    color = request.form.get('color', '').strip() or DEFAULT_SHIFT_COLOR
    start_t = _parse_hhmm(request.form.get('start_time'))
    end_t = _parse_hhmm(request.form.get('end_time'))
    breaks_min = request.form.get('breaks_minutes', type=int)
    sort_order = request.form.get('sort_order', type=int)

    breaks_min = 0 if breaks_min is None or breaks_min < 0 else breaks_min
    sort_order = 0 if sort_order is None else sort_order
    if not _HEX_COLOR_RE.match(color):
        color = DEFAULT_SHIFT_COLOR

    # ── Validación ───────────────────────────────────────────────────────────
    if not name or not short_name:
        flash('El nombre y la abreviatura son obligatorios.', 'danger')
        return redirect(url_for('shifts.manage_shift_types'))

    if len(name) > 50 or len(short_name) > 5:
        flash('El nombre admite 50 caracteres y la abreviatura 5.', 'danger')
        return redirect(url_for('shifts.manage_shift_types'))

    if start_t is None or end_t is None:
        flash('Las horas de inicio y fin son obligatorias, en formato HH:MM.', 'danger')
        return redirect(url_for('shifts.manage_shift_types'))

    # end < start es válido: es el turno de noche (ver ShiftAssignment.net_hours).
    # end == start no lo es: net_hours lo interpretaría como un turno de 24 h.
    if start_t == end_t:
        flash('La hora de fin no puede ser igual a la de inicio.', 'danger')
        return redirect(url_for('shifts.manage_shift_types'))

    duration_min = ((end_t.hour * 60 + end_t.minute)
                    - (start_t.hour * 60 + start_t.minute)) % (24 * 60)
    if breaks_min >= duration_min:
        flash('El descanso no puede ser igual o superior a la duración del turno.', 'danger')
        return redirect(url_for('shifts.manage_shift_types'))

    # ── Localizar el registro a editar ───────────────────────────────────────
    st = db.session.get(ShiftType, int(st_id)) if st_id.isdigit() else None
    if st_id and not st:
        flash('El tipo de turno indicado ya no existe.', 'warning')
        return redirect(url_for('shifts.manage_shift_types'))

    # ── Nombre único (ShiftType.name es unique) ──────────────────────────────
    dup_q = ShiftType.query.filter(func.lower(ShiftType.name) == name.lower())
    if st:
        dup_q = dup_q.filter(ShiftType.id != st.id)
    if dup_q.first():
        flash('Ya existe un tipo de turno con ese nombre.', 'danger')
        return redirect(url_for('shifts.manage_shift_types'))

    # Donde se dibuja en el tablero. Vacio = no sale, que es como se quedan los
    # turnos que ya existian.
    board_row = request.form.get('board_row') or None
    if board_row not in FILAS_TABLERO:
        board_row = None

    is_new = st is None
    if is_new:
        st = ShiftType(name=name, short_name=short_name, color=color,
                       start_time=start_t, end_time=end_t,
                       breaks_minutes=breaks_min, sort_order=sort_order,
                       active=True)
        db.session.add(st)
        ok, err = _safe_flush('No se pudo crear el tipo de turno.')
        if not ok:
            flash(err, 'danger')
            return redirect(url_for('shifts.manage_shift_types'))
    else:
        st.name = name
        st.short_name = short_name
        st.color = color
        st.start_time = start_t
        st.end_time = end_t
        st.breaks_minutes = breaks_min
        st.sort_order = sort_order
        # `active` no se toca aquí: lo gestiona toggle_shift_type_active.
    st.board_row = board_row

    log_audit('create' if is_new else 'update', 'shift_type', st.id, {
        'name': name, 'short_name': short_name,
        'start_time': start_t.strftime('%H:%M'),
        'end_time': end_t.strftime('%H:%M'),
        'breaks_minutes': breaks_min, 'sort_order': sort_order,
        'board_row': board_row,
    })

    ok, err = _safe_commit('No se pudo guardar el tipo de turno.')
    if not ok:
        flash(err, 'danger')
        return redirect(url_for('shifts.manage_shift_types'))

    flash('Tipo de turno creado.' if is_new else 'Tipo de turno actualizado.', 'success')
    return redirect(url_for('shifts.manage_shift_types'))


@bp.route('/shift-types/delete/<int:id>', methods=['POST'])
@admin_required
def delete_shift_type(id):
    st = db.session.get(ShiftType, id)
    if not st:
        flash('El tipo de turno ya no existe.', 'warning')
        return redirect(url_for('shifts.manage_shift_types'))

    blockers = []
    n_assign = ShiftAssignment.query.filter_by(shift_type_id=st.id).count()
    if n_assign:
        blockers.append(f'{n_assign} asignaciones de cuadrante')
    n_days = RotationPatternDay.query.filter_by(shift_type_id=st.id).count()
    if n_days:
        blockers.append(f'{n_days} dias de patrones de rotacion')
    n_cfg = WorkerShiftConfig.query.filter_by(fixed_shift_type_id=st.id).count()
    if n_cfg:
        blockers.append(f'{n_cfg} configuraciones de trabajadoras')

    if blockers:
        flash(f'No se puede eliminar «{st.name}»: esta en uso ({", ".join(blockers)}). '
              'Desactivalo en su lugar.', 'warning')
        return redirect(url_for('shifts.manage_shift_types'))

    # Los requisitos de cobertura son configuración pura y no tienen sentido sin
    # su tipo de turno: se eliminan junto con él.
    ShiftCoverageRequirement.query.filter_by(shift_type_id=st.id).delete(
        synchronize_session=False)

    log_audit('delete', 'shift_type', st.id,
              {'name': st.name, 'short_name': st.short_name})
    db.session.delete(st)

    ok, err = _safe_commit('No se pudo eliminar el tipo de turno.')
    if not ok:
        flash(err, 'danger')
        return redirect(url_for('shifts.manage_shift_types'))

    flash('Tipo de turno eliminado.', 'success')
    return redirect(url_for('shifts.manage_shift_types'))


@bp.route('/shift-types/toggle-active', methods=['POST'])
@admin_required
def toggle_shift_type_active():
    data = request.get_json(silent=True) or {}
    try:
        st_id = int(data.get('id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Identificador no valido'}), 400

    st = db.session.get(ShiftType, st_id)
    if not st:
        return jsonify({'error': 'Tipo de turno no encontrado'}), 404

    st.active = bool(data.get('active', True))
    log_audit('update', 'shift_type', st.id, {'active': st.active})

    ok, err = _safe_commit('No se pudo actualizar el estado del tipo de turno.')
    if not ok:
        return jsonify({'error': err}), 500

    return jsonify({'ok': True, 'active': st.active})


# ── API WORKER: MIS TURNOS ──────────────────────────────────────────────────

@bp.route('/api/worker/my-shifts')
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
            return jsonify({'error': 'Formato de mes no valido (YYYY-MM)'}), 400
        if not (1 <= mo <= 12) or not (2000 <= year <= 2100):
            return jsonify({'error': 'Formato de mes no valido (YYYY-MM)'}), 400
    else:
        year, mo = datetime.now().year, datetime.now().month

    import calendar
    num_days = calendar.monthrange(year, mo)[1]
    first_day = date(year, mo, 1)
    last_day = date(year, mo, num_days)

    assignments = ShiftAssignment.query.options(
        joinedload(ShiftAssignment.shift_type), joinedload(ShiftAssignment.position)
    ).filter(
        ShiftAssignment.cleaner_id == worker_id,
        ShiftAssignment.date >= first_day,
        ShiftAssignment.date <= last_day,
    ).order_by(ShiftAssignment.date).all()

    # Las ausencias importan tanto como los turnos: quien esta de vacaciones
    # quiere verlo en su mes, no deducirlo de que no tiene nada puesto.
    ausencias = _ausencias_de(first_day, last_day).get(worker_id, {})

    result = []
    for a in assignments:
        st = a.shift_type
        result.append({
            'date': a.date.isoformat(),
            'position': a.position.code if a.position else None,
            'shift': {
                'name': st.name if st else 'Libre',
                'short_name': st.short_name if st else 'L',
                'color': st.color if st else '#dee2e6',
                'start_time': st.start_time.strftime('%H:%M') if st else None,
                'end_time': st.end_time.strftime('%H:%M') if st else None,
            } if st else None,
        })
    return jsonify({
        'year': year, 'month': mo, 'shifts': result,
        'absences': [{'date': dia.isoformat(), 'name': tipo.name,
                      'short_name': tipo.short_name, 'color': tipo.color}
                     for dia, tipo in sorted(ausencias.items())],
    })


# ─── Phase 2 & 3: Rotation Patterns, Absences, Validation ─────────────────────

@bp.route('/cuadrantes/patrones')
@admin_required
def manage_patterns():
    patterns = RotationPattern.query.order_by(RotationPattern.name).all()
    shift_types = ShiftType.query.filter_by(active=True).order_by(ShiftType.sort_order).all()
    return render_template('manage_patterns.html', patterns=patterns, shift_types=shift_types)


@bp.route('/cuadrantes/patrones/add_edit', methods=['POST'])
@admin_required
def add_edit_pattern():
    pattern_id = request.form.get('pattern_id', '').strip()
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    cycle_days = request.form.get('cycle_days', type=int) or 0

    if not name:
        flash('El nombre del patron es obligatorio.', 'danger')
        return redirect(url_for('shifts.manage_patterns'))

    if not (1 <= cycle_days <= 31):
        flash('Los dias del ciclo deben estar entre 1 y 31.', 'danger')
        return redirect(url_for('shifts.manage_patterns'))

    if pattern_id and not pattern_id.isdigit():
        flash('El patron indicado no es valido.', 'danger')
        return redirect(url_for('shifts.manage_patterns'))

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

    ok, err = _safe_commit('No se pudo guardar el patron.')
    if not ok:
        flash(err, 'danger')
        return redirect(url_for('shifts.manage_patterns'))
    flash('Patron guardado correctamente.', 'success')
    return redirect(url_for('shifts.manage_patterns'))


@bp.route('/cuadrantes/patrones/delete/<int:id>', methods=['POST'])
@admin_required
def delete_pattern(id):
    pattern = db.session.get(RotationPattern, id)
    if pattern:
        if pattern.worker_configs:
            flash('No se puede eliminar: hay trabajadores asignados a este patron.', 'danger')
        else:
            db.session.delete(pattern)
            ok, err = _safe_commit('No se pudo eliminar el patron.')
            flash('Patron eliminado.' if ok else err, 'success' if ok else 'danger')
    return redirect(url_for('shifts.manage_patterns'))


@bp.route('/cuadrantes/worker-config', methods=['POST'])
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
    ok, err = _safe_commit('No se pudo guardar la configuracion.')
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'ok': True})


@bp.route('/cuadrantes/generate', methods=['POST'])
@admin_required
def cuadrantes_generate():
    """Generate shift assignments using the SmartScheduler."""
    from ..scheduler import SmartScheduler
    data = request.get_json()
    year = data.get('year')
    month = data.get('month')
    preserve_overrides = data.get('preserve_overrides', True)
    fill_gaps = data.get('fill_gaps', True)
    keep_all_existing = data.get('keep_all_existing', False)

    scheduler = SmartScheduler(year, month)
    result = scheduler.generate(
        preserve_overrides=preserve_overrides,
        fill_gaps=fill_gaps,
        keep_all_existing=keep_all_existing,
        created_by_id=current_user.id,
    )
    return jsonify(result)


@bp.route('/cuadrantes/fill-gap', methods=['POST'])
@admin_required
def cuadrantes_fill_gap():
    """Fill a single coverage gap on a specific day/shift."""
    from ..scheduler import SmartScheduler
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
        ok, err = _safe_commit('No se pudo asignar el hueco.')
        if not ok:
            return jsonify({'error': err}), 500
        st = db.session.get(ShiftType, shift_type_id)
        return jsonify({
            'ok': True,
            'worker_id': best_worker.id,
            'worker_name': best_worker.name,
            'short_name': st.short_name if st else '',
            'color': st.color if st else '',
        })
    return jsonify({'ok': False, 'error': 'No hay trabajadores disponibles para cubrir este turno.'}), 404


@bp.route('/cuadrantes/coverage-settings')
@admin_required
def coverage_settings():
    shift_types = ShiftType.query.filter_by(active=True).order_by(ShiftType.sort_order).all()
    requirements = ShiftCoverageRequirement.query.all()
    req_map = {(r.shift_type_id, r.day_type): r for r in requirements}
    return render_template('coverage_settings.html', shift_types=shift_types, req_map=req_map)


@bp.route('/cuadrantes/coverage-settings/save', methods=['POST'])
@admin_required
def save_coverage_settings():
    shift_types = ShiftType.query.filter_by(active=True).all()
    for st in shift_types:
        for day_type in ['weekday', 'weekend']:
            min_val = request.form.get(f'min_{st.id}_{day_type}', '', type=int)
            ideal_val = request.form.get(f'ideal_{st.id}_{day_type}', '', type=int)
            existing = ShiftCoverageRequirement.query.filter_by(
                shift_type_id=st.id, day_type=day_type
            ).first()
            if min_val and min_val > 0:
                if existing:
                    existing.min_workers = min_val
                    existing.ideal_workers = ideal_val if ideal_val and ideal_val > 0 else None
                else:
                    db.session.add(ShiftCoverageRequirement(
                        shift_type_id=st.id, day_type=day_type, min_workers=min_val,
                        ideal_workers=ideal_val if ideal_val and ideal_val > 0 else None,
                    ))
            elif existing:
                db.session.delete(existing)
    ok, err = _safe_commit('No se pudieron guardar los requisitos de cobertura.')
    if not ok:
        flash(err, 'danger')
        return redirect(url_for('shifts.coverage_settings'))
    flash('Requisitos de cobertura guardados.', 'success')
    return redirect(url_for('shifts.coverage_settings'))


@bp.route('/cuadrantes/ausencias')
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


@bp.route('/cuadrantes/ausencias/add_edit', methods=['POST'])
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
        return redirect(url_for('shifts.manage_absences'))

    start_d = date.fromisoformat(start_date_str)
    end_d = date.fromisoformat(end_date_str)

    if end_d < start_d:
        flash('La fecha fin debe ser posterior a la fecha inicio.', 'danger')
        return redirect(url_for('shifts.manage_absences'))

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

    ok, err = _safe_commit('No se pudo registrar la ausencia.')
    if not ok:
        flash(err, 'danger')
        return redirect(url_for('shifts.manage_absences'))
    flash('Ausencia registrada.', 'success')
    return redirect(url_for('shifts.manage_absences'))


@bp.route('/cuadrantes/ausencias/delete/<int:id>', methods=['POST'])
@admin_required
def delete_absence(id):
    absence = db.session.get(Absence, id)
    if absence:
        db.session.delete(absence)
        ok, err = _safe_commit('No se pudo eliminar la ausencia.')
        flash('Ausencia eliminada.' if ok else err, 'success' if ok else 'danger')
    return redirect(url_for('shifts.manage_absences'))


@bp.route('/cuadrantes/validate')
@admin_required
def cuadrantes_validate():
    """Validate shift assignments for labor law compliance. Returns warnings."""
    year = request.args.get('year', type=int) or datetime.now().year
    month = request.args.get('month', type=int) or datetime.now().month
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        return jsonify({'error': 'Ano o mes no valido'}), 400

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
                                'message': f'Solo {rest_hours:.0f}h de descanso entre turnos (minimo 12h)',
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
                    'message': f'{week_hours:.1f}h en semana {monday.strftime("%d/%m")}-{sunday.strftime("%d/%m")} (maximo 40h)',
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
                    'message': f'Sin dia libre en semana {monday.strftime("%d/%m")}-{sunday.strftime("%d/%m")}',
                })

    return jsonify({'warnings': warnings})


@bp.route('/api/shifts/ai-suggestions', methods=['POST'])
@limiter.limit("5/minute")
@admin_required
def ai_shift_suggestions():
    """Use AI to analyze shift patterns and suggest improvements."""
    from .. import app
    api_key = app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'IA no disponible'}), 503

    data = request.get_json() or {}
    year = data.get('year', date.today().year)
    month = data.get('month', date.today().month)
    import calendar
    num_days = calendar.monthrange(year, month)[1]
    first = date(year, month, 1)
    last = date(year, month, num_days)

    # Gather data
    workers = Cleaner.query.filter_by(active=True).filter(Cleaner.role != 'gestion').order_by(Cleaner.name).all()
    shift_types = {st.id: st for st in ShiftType.query.filter_by(active=True).all()}
    assignments = ShiftAssignment.query.filter(ShiftAssignment.date >= first, ShiftAssignment.date <= last).all()
    absences = Absence.query.filter(Absence.start_date <= last, Absence.end_date >= first).all()
    reqs = ShiftCoverageRequirement.query.all()

    # Absence patterns (last 6 months)
    abs_6m = Absence.query.filter(Absence.start_date >= date.today() - timedelta(days=180)).all()
    absence_summary = {}
    for a in abs_6m:
        w = a.cleaner
        if w:
            absence_summary.setdefault(w.name, []).append(f"{a.start_date.strftime('%d/%m')}-{a.end_date.strftime('%d/%m')} ({a.absence_type.name if a.absence_type else '?'})")

    # Current coverage
    assign_map = {}
    for a in assignments:
        key = (a.date.isoformat(), a.shift_type_id)
        assign_map[key] = assign_map.get(key, 0) + 1

    lines = [f"QUADRANT {month:02d}/{year} — {len(workers)} treballadors, {num_days} dies"]

    # Coverage gaps
    gaps = []
    for d in range(1, num_days + 1):
        target_date = date(year, month, d)
        is_weekend = target_date.weekday() >= 5
        for st_id, st in shift_types.items():
            count = assign_map.get((target_date.isoformat(), st_id), 0)
            for req in reqs:
                if req.shift_type_id == st_id:
                    day_type = 'weekend' if is_weekend else 'weekday'
                    if req.day_type in (day_type, 'all') and count < req.min_workers:
                        gaps.append(f"  {target_date.strftime('%d/%m')} {st.short_name}: {count}/{req.min_workers}")

    if gaps:
        lines.append(f"\nGAPS DE COBERTURA ({len(gaps)}):")
        for g in gaps[:15]:
            lines.append(g)

    # Worker hours
    worker_hours = {}
    for a in assignments:
        if a.shift_type_id and a.shift_type_id in shift_types:
            st = shift_types[a.shift_type_id]
            hours = ((datetime.combine(date.today(), st.end_time) - datetime.combine(date.today(), st.start_time)).total_seconds() / 3600)
            if hours < 0:
                hours += 24
            worker_hours[a.cleaner_id] = worker_hours.get(a.cleaner_id, 0) + hours

    lines.append(f"\nHORES PER TREBALLADOR:")
    for w in workers:
        h = worker_hours.get(w.id, 0)
        lines.append(f"  {w.name}: {h:.0f}h")

    if absence_summary:
        lines.append(f"\nPATRONS D'ABSENCIA (6 mesos):")
        for name, abs_list in absence_summary.items():
            lines.append(f"  {name}: {', '.join(abs_list[:5])}")

    context = '\n'.join(lines)
    system = """Eres un consultor de planificacion de turnos para la residencia La Vila Gran.
Analiza los datos del cuadrante y da sugerencias concretas y accionables.
Responde en espanol, formato texto breve con viñetas.
Busca: gaps de cobertura, desequilibrios de horas, patrones de absentismo,
trabajadores sobrecargados o infrautilizados, y mejoras de distribucion."""

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=800,
            system=system,
            messages=[{'role': 'user', 'content': f'Analiza este cuadrante:\n\n{context}'}],
        )
        text = ''.join(b.text for b in resp.content if hasattr(b, 'text'))
    except Exception as e:
        app.logger.error('Error al sugerir turnos con IA: %s', e)
        return jsonify({'error': 'No se han podido generar las sugerencias de turnos.'}), 500

    return jsonify({'suggestions': text})


@bp.route('/api/shifts/ai-suggest-replacement', methods=['POST'])
@limiter.limit("5/minute")
@admin_required
def ai_suggest_replacement():
    """AI suggests best replacement worker for a vacant shift."""
    from .. import app
    from ..chatbot import _sugerir_cobertura
    import json as _json

    data = request.get_json() or {}
    fecha = data.get('date', '')
    turno = data.get('shift_short_name', '')
    motivo = data.get('reason', '')

    if not fecha:
        return jsonify({'error': 'Fecha requerida'}), 400

    result = _json.loads(_sugerir_cobertura(fecha, turno, motivo))

    # If we have candidates and AI key, get AI reasoning
    api_key = app.config.get('ANTHROPIC_API_KEY')
    if api_key and result.get('candidatos'):
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001', max_tokens=400,
                system='Eres un asistente de planificacion de turnos. Analiza los candidatos y recomienda el mejor, explicando brevemente por que. Responde en español, formato breve.',
                messages=[{'role': 'user', 'content': f'Necesito cobertura para {fecha} turno {turno}. Motivo: {motivo}.\n\nCandidatos:\n{_json.dumps(result["candidatos"], ensure_ascii=False)}'}],
            )
            reasoning = ''.join(b.text for b in resp.content if hasattr(b, 'text'))
            result['ai_reasoning'] = reasoning
        except Exception:
            pass

    return jsonify(result)


# ── TABLERO DE TURNOS ───────────────────────────────────────────────────────
# La rejilla mensual contesta «cuantas horas lleva Maria y queda cubierto el
# turno de noche». El tablero contesta otra pregunta, que es la que se hace al
# planificar: «quien hace M1 el martes». Por eso conviven: cada una sirve para
# una cosa y ninguna sustituye a la otra.

# Colores de reserva para quien no tenga uno elegido. Son los de su Excel:
# tienen que distinguirse de un vistazo en una casilla de dos centimetros, no
# ser bonitos. Se reparten por id, asi que a nadie le cambia el suyo.
PALETA_PERSONAL = [
    '#8ab4e8', '#f2e14c', '#8fe3b0', '#3f7d3f', '#c9ccd1',
    '#b07d12', '#b9a7e8', '#ef5aa8', '#5ec8d8', '#f0a01e',
    '#e03131', '#1565c0', '#5c6670', '#6a1b9a', '#f3a6a6',
]

# El segundo eje, para que dos personas nunca se pinten igual. Quince colores
# por cuatro rellenos son sesenta combinaciones distintas.
PATRONES_PERSONAL = ['liso', 'rayas', 'puntos', 'malla']
ETIQUETAS_PATRON = {'liso': 'Liso', 'rayas': 'Rayas',
                    'puntos': 'Puntos', 'malla': 'Cuadricula'}

FILAS_TABLERO = ['manana', 'tarde', 'noche', 'lateral']
ETIQUETAS_FILA = {'manana': 'Mañana', 'tarde': 'Tarde',
                  'noche': 'Noche', 'lateral': 'Otros puestos'}

DIAS_SEMANA = ['Lunes', 'Martes', 'Miercoles', 'Jueves', 'Viernes', 'Sabado', 'Domingo']
# A mano y no con strftime('%B'): eso sale en el idioma del sistema, que en el
# contenedor es ingles. El resto del modulo ya lleva los dias asi.
MESES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio',
         'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']


def _color_trabajadora(trabajadora) -> str:
    """Su color, o uno estable derivado del id si nadie se lo ha puesto."""
    if trabajadora.color and _HEX_COLOR_RE.match(trabajadora.color):
        return trabajadora.color
    return PALETA_PERSONAL[(trabajadora.id or 0) % len(PALETA_PERSONAL)]


def _patron_trabajadora(trabajadora) -> str:
    """Como se rellena su recuadro.

    El color solo no llega: quince colores para treinta personas repiten por
    fuerza, y dos naranjas iguales en la vista del mes no se distinguen, que es
    justo para lo que sirve esa vista. El relleno multiplica por cuatro las
    combinaciones, y ademas sobrevive a una impresion en blanco y negro y a una
    persona que no distingue el rojo del verde.

    Derivado del id, no del puesto en una lista: asi el de cada una no cambia
    cuando entra o se va alguien. Aprenderselo es el objetivo.
    """
    if trabajadora.pattern in PATRONES_PERSONAL:
        return trabajadora.pattern
    vuelta = (trabajadora.id or 0) // len(PALETA_PERSONAL)
    return PATRONES_PERSONAL[vuelta % len(PATRONES_PERSONAL)]


TINTA_CLARA = 'rgba(255,255,255,.62)'
TINTA_OSCURA = 'rgba(0,0,0,.34)'


def _luminancia(r: float, g: float, b: float) -> float:
    def lineal(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lineal(r) + 0.7152 * lineal(g) + 0.0722 * lineal(b)


def _tinta_sobre(hex_color: str) -> str:
    """De que color van las rayas encima de ese fondo.

    Rayas blancas sobre un amarillo claro no se ven, y negras sobre un azul
    oscuro tampoco. En vez de un umbral a ojo, se prueban las dos y se queda la
    que mas se separa del fondo: la tinta es translucida, asi que el resultado
    depende tanto del color como de la opacidad, y con los azules medios de la
    paleta un umbral fijo se equivoca.
    """
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except (ValueError, IndexError, TypeError):
        return TINTA_CLARA

    fondo = _luminancia(r, g, b)
    # La tinta se mezcla con el fondo segun su opacidad.
    clara = _luminancia(*(c * (1 - .62) + .62 for c in (r, g, b)))
    oscura = _luminancia(*(c * (1 - .34) for c in (r, g, b)))
    return (TINTA_CLARA if abs(clara - fondo) >= abs(oscura - fondo)
            else TINTA_OSCURA)


def _pinta(trabajadora) -> dict:
    """Con que se pinta a esta persona, en todas partes igual."""
    color = _color_trabajadora(trabajadora)
    return {'color': color,
            'patron': _patron_trabajadora(trabajadora),
            'tinta': _tinta_sobre(color)}


def _pares_de_pintura():
    """Todas las combinaciones, color primero.

    Primero los quince colores lisos, que son los mas faciles de reconocer;
    los rellenos entran cuando los colores se acaban.
    """
    for patron in PATRONES_PERSONAL:
        for color in PALETA_PERSONAL:
            yield color, patron


def _choques_de_pintura() -> list:
    """Grupos de personas en activo que se pintan exactamente igual.

    Devuelve [[Cleaner, Cleaner], ...]. Dos naranjas lisos son dos nombres que
    no se pueden distinguir mirando el mes, que es lo que se mira para repartir
    el trabajo.
    """
    por_par: dict = {}
    for c in Cleaner.query.filter_by(active=True).order_by(Cleaner.id).all():
        por_par.setdefault(
            (_color_trabajadora(c), _patron_trabajadora(c)), []).append(c)
    return [gente for gente in por_par.values() if len(gente) > 1]


def _repartir_pintura() -> list:
    """Da una combinacion libre a quien se pinte como otra. Devuelve los nombres.

    No hace commit: lo hace quien llama, para que entre en la misma transaccion
    que su registro de auditoria.
    """
    usados = set()
    cambiadas = []
    for c in Cleaner.query.filter_by(active=True).order_by(Cleaner.id).all():
        par = (_color_trabajadora(c), _patron_trabajadora(c))
        if par not in usados:
            usados.add(par)
            continue
        # Dentro de un choque, la primera se queda como estaba: el color se
        # aprende y moverselo a todo el mundo seria peor que el problema.
        libre = next((p for p in _pares_de_pintura() if p not in usados), None)
        if not libre:
            # Sesenta combinaciones agotadas. No pasa hoy, pero callarlo seria
            # dejar a alguien repetido sin decirlo.
            break
        c.color, c.pattern = libre
        usados.add(libre)
        cambiadas.append(c.name)
    return cambiadas


def _lunes_de(dia: date) -> date:
    return dia - timedelta(days=dia.weekday())


def _dia_o_hoy(iso: str | None) -> date:
    """La fecha del parametro, o hoy si no viene o viene mal."""
    try:
        return date.fromisoformat(iso) if iso else date.today()
    except ValueError:
        return date.today()


def _ausencias_de(desde: date, hasta: date) -> dict:
    """{cleaner_id: {dia: AbsenceType}} del rango, con las bajas desplegadas."""
    filas = Absence.query.options(joinedload(Absence.absence_type)).filter(
        Absence.start_date <= hasta, Absence.end_date >= desde
    ).all()
    mapa: dict = {}
    for a in filas:
        dia = max(a.start_date, desde)
        fin = min(a.end_date, hasta)
        while dia <= fin:
            mapa.setdefault(a.cleaner_id, {})[dia] = a.absence_type
            dia += timedelta(days=1)
    return mapa


def _esta_ausente(cleaner_id: int, dia: date) -> bool:
    return db.session.query(Absence.id).filter(
        Absence.cleaner_id == cleaner_id,
        Absence.start_date <= dia,
        Absence.end_date >= dia,
    ).first() is not None


def _puestos_activos():
    """Los puestos que se dibujan, con su turno, ordenados como en el recuadro."""
    return ShiftPosition.query.options(
        joinedload(ShiftPosition.shift_type)
    ).join(ShiftType).filter(
        ShiftPosition.active.is_(True),
        ShiftType.active.is_(True),
        ShiftType.board_row.isnot(None),
    ).order_by(ShiftType.sort_order, ShiftType.id,
               ShiftPosition.sort_order, ShiftPosition.id).all()


def _personal_del_tablero():
    """Quien puede salir en el tablero. Gestion no hace turnos de planta."""
    return Cleaner.query.filter(
        Cleaner.active.is_(True), Cleaner.role != 'gestion'
    ).order_by(Cleaner.name).all()


def _ficha(asignacion, ausencias) -> dict | None:
    """La persona que ocupa una casilla, tal y como la pinta el tablero."""
    if not asignacion or not asignacion.cleaner:
        return None
    c = asignacion.cleaner
    return {'assignment_id': asignacion.id, 'cleaner_id': c.id, 'name': c.name,
            **_pinta(c)}


def _minutos(t) -> int:
    return t.hour * 60 + t.minute


def _tramo(st, previo: bool = False) -> tuple[int, int]:
    """El rato que ocupa un turno, en minutos desde las 00:00 del dia que se mira.

    La noche cruza la medianoche, asi que el final se pasa al dia siguiente. Y
    la que viene de la vispera se mide en negativo: una noche de 21:00 a 08:00
    de ayer ocupa de -180 a 480, o sea entra tres horas antes de que empiece el
    dia y sale a las ocho de la manana.
    """
    ini, fin = _minutos(st.start_time), _minutos(st.end_time)
    if fin <= ini:
        fin += 1440
    return (ini - 1440, fin - 1440) if previo else (ini, fin)


def _hhmm(minutos: int) -> str:
    return f'{(minutos // 60) % 24:02d}:{minutos % 60:02d}'


def _casilla(p, dia: date, asignaciones: dict, ausencias: dict,
             reflejo: bool = False) -> dict:
    st = p.shift_type
    previo = reflejo and p.echo_previous_day
    ini, fin = _tramo(st, previo)
    etiqueta = p.code + (' dia-1' if previo else '')
    return {
        'position_id': p.id,
        'code': etiqueta,
        'name': p.name or '',
        'shift_name': st.name,
        'shift_color': st.color,
        'horario': f'{_hhmm(ini)}-{_hhmm(fin)}',
        'inicio': ini,
        'fin': fin,
        'solo_lectura': reflejo,
        'reflejo': reflejo,
        # El de ayer no se puede refrescar desde esta pagina: su casilla de
        # verdad es la del dia anterior, que aqui no esta.
        'reflejo_de_ayer': previo,
        'ocupa': _ficha(asignaciones.get((dia - timedelta(days=1) if previo else dia, p.id)),
                        ausencias),
    }


def _eje_del_dia(cajas: list) -> dict:
    """El eje de horas del recuadro.

    Va desde una hora antes de que entre el primer turno del dia hasta que sale
    el ultimo. Lo que viene de la noche anterior entra por arriba recortado: a
    escala de las 24 horas, una noche de once horas seria la caja mas alta del
    recuadro y taparia lo que de verdad se esta mirando, que es la jornada.
    """
    # Un recuadro sin nada todavia tambien tiene que dibujarse: se le da una
    # jornada de oficina y ya se ajustara cuando haya puestos.
    if not cajas:
        cajas = [{'inicio': 7 * 60, 'fin': 15 * 60}]
    propias = [c for c in cajas if c['inicio'] >= 0] or cajas
    ini = (min(c['inicio'] for c in propias) // 60) * 60 - 60
    fin = -(-max(c['fin'] for c in cajas) // 60) * 60
    if fin <= ini:
        fin = ini + 60
    rango = fin - ini
    # Con muchas horas, una raya cada hora es ruido: se marca cada dos.
    paso = 60 if rango <= 14 * 60 else 120
    horas = [{'etiqueta': _hhmm(m), 'pct': (m - ini) * 100.0 / rango}
             for m in range(ini, fin + 1, paso)]
    return {'inicio': ini, 'fin': fin, 'rango': rango, 'horas': horas}


def _situar(caja: dict, eje: dict) -> dict:
    """Donde empieza y cuanto ocupa la caja dentro del eje, en porcentaje."""
    ini = max(caja['inicio'], eje['inicio'])
    fin = min(caja['fin'], eje['fin'])
    caja['recortado'] = caja['inicio'] < eje['inicio']
    caja['top'] = round((ini - eje['inicio']) * 100.0 / eje['rango'], 3)
    caja['alto'] = round(max(fin - ini, 30) * 100.0 / eje['rango'], 3)
    return caja


def _columnas_del_dia(filas: dict) -> list:
    """Reparte las cajas en columnas, como en el cuadrante de papel.

    La manana va encima de la tarde en la misma columna porque no se solapan en
    el tiempo: M1 y T1 son la misma plaza a dos ratos del dia. Lo que viene de
    la noche va a su propia columna a la izquierda, y los puestos de fuera
    (cocina, recepcion) se reparten a los dos lados.
    """
    columnas = []

    # Los reflejos: normalmente uno encima del otro en una sola columna (la
    # noche que acaba por la manana y la que entra por la tarde no coinciden).
    for reflejo in sorted(filas['reflejos'], key=lambda c: c['inicio']):
        for col in columnas:
            if reflejo['inicio'] >= col[-1]['fin']:
                col.append(reflejo)
                break
        else:
            columnas.append([reflejo])

    manana, tarde = filas['manana'], filas['tarde']
    for i in range(max(len(manana), len(tarde))):
        col = []
        if i < len(manana):
            col.append(manana[i])
        if i < len(tarde):
            col.append(tarde[i])
        columnas.append(col)

    laterales = filas['lateral']
    mitad = (len(laterales) + 1) // 2
    izquierda = [[c] for c in laterales[:mitad]]
    derecha = [[c] for c in laterales[mitad:]]
    return izquierda + columnas + derecha


def _tablero_de_dia(dia: date, puestos, asignaciones: dict, ausencias: dict) -> dict:
    """El recuadro de un dia: un eje de horas y las cajas colocadas en el.

    Cada caja empieza y acaba donde dice su horario, asi que se ve de un vistazo
    quien entra antes y quien pliega primero. Los reflejos son la misma persona
    ensenada donde tambien esta —quien entra por la tarde esta toda la tarde, y
    quien hizo la noche sigue aqui media manana del dia siguiente—, por eso son
    de solo lectura: se cambian en su casilla de la franja de noche.
    """
    filas = {f: [] for f in FILAS_TABLERO}
    filas['reflejos'] = []
    for p in puestos:
        filas[p.shift_type.board_row].append(_casilla(p, dia, asignaciones, ausencias))
        if p.echo_row in FILAS_TABLERO:
            filas['reflejos'].append(
                _casilla(p, dia, asignaciones, ausencias, reflejo=True))

    # La franja de noche va aparte, abajo: es quien entra esta noche, y a escala
    # se saldria del eje de la jornada.
    jornada = filas['manana'] + filas['tarde'] + filas['lateral'] + filas['reflejos']
    eje = _eje_del_dia(jornada)
    for caja in jornada:
        _situar(caja, eje)

    return {'fecha': dia, 'iso': dia.isoformat(), 'dow': DIAS_SEMANA[dia.weekday()],
            'eje': eje, 'columnas': _columnas_del_dia(filas),
            'noche': filas['noche'], 'filas': filas}


def _asignaciones_por_puesto(desde: date, hasta: date) -> dict:
    """{(fecha, position_id): ShiftAssignment} del rango, solo las que ocupan puesto."""
    filas = ShiftAssignment.query.options(
        joinedload(ShiftAssignment.cleaner)
    ).filter(
        ShiftAssignment.date >= desde, ShiftAssignment.date <= hasta,
        ShiftAssignment.position_id.isnot(None),
    ).all()
    return {(a.date, a.position_id): a for a in filas}


def _tablero_de_rango(desde: date, hasta: date) -> list:
    """Un recuadro por dia, de golpe.

    Se consulta todo el rango de una vez: un mes son hasta cuarenta y dos dias y
    hacerlo dia a dia serian cuarenta y dos viajes a la base de datos para
    pintar una sola pantalla.
    """
    puestos = _puestos_activos()
    # Un dia antes, por las casillas que ensenan la noche de la vispera.
    vispera = desde - timedelta(days=1)
    asignaciones = _asignaciones_por_puesto(vispera, hasta)
    ausencias = _ausencias_de(vispera, hasta)
    return [_tablero_de_dia(desde + timedelta(days=i), puestos, asignaciones, ausencias)
            for i in range((hasta - desde).days + 1)]


def _tablero_de_semana(lunes: date) -> list:
    """Los siete recuadros de una semana."""
    return _tablero_de_rango(lunes, lunes + timedelta(days=6))


def _leyenda_de(dias: list) -> list:
    """Quien sale en estos dias, para la leyenda de colores."""
    vistos = {}
    for d in dias:
        for nombre, fila in d['filas'].items():
            if nombre == 'reflejos':
                continue
            for casilla in fila:
                if casilla['ocupa']:
                    vistos[casilla['ocupa']['cleaner_id']] = casilla['ocupa']
    return sorted(vistos.values(), key=lambda f: f['name'])


def _estado_semana(lunes: date):
    """(publicacion, modificada_despues) de una semana."""
    pub = ShiftWeekPublication.query.filter_by(week_start=lunes).first()
    if not pub:
        return None, False
    domingo = lunes + timedelta(days=6)
    tocada = ShiftAssignment.query.filter(
        ShiftAssignment.date >= lunes, ShiftAssignment.date <= domingo,
        func.coalesce(ShiftAssignment.updated_at,
                      ShiftAssignment.created_at) > pub.published_at,
    ).first()
    return pub, tocada is not None


@bp.route('/cuadrantes/tablero')
@admin_required
def shift_board_week():
    """La semana entera, como el Excel que usan hoy."""
    lunes = _lunes_de(_dia_o_hoy(request.args.get('semana')))
    dias = _tablero_de_semana(lunes)
    pub, modificada = _estado_semana(lunes)

    return render_template(
        'shift_board_week.html',
        lunes=lunes, domingo=lunes + timedelta(days=6), dias=dias,
        filas=FILAS_TABLERO, etiquetas_fila=ETIQUETAS_FILA,
        # Solo quien sale esa semana: una leyenda con toda la plantilla no
        # ayuda a leer nada.
        leyenda=_leyenda_de(dias),
        publicacion=pub, modificada=modificada,
        # Cuantas lo han confirmado: es la pregunta que se hace quien planifica
        # en cuanto ha mandado los horarios.
        acuses=_resumen_acuses(lunes),
        semana_anterior=(lunes - timedelta(days=7)).isoformat(),
        semana_siguiente=(lunes + timedelta(days=7)).isoformat(),
        hay_puestos=bool(_puestos_activos()),
    )


@bp.route('/cuadrantes/tablero/dia/<iso>')
@admin_required
def shift_board_day(iso: str):
    """El recuadro de un dia, que es donde se arrastra."""
    dia = _dia_o_hoy(iso)
    puestos = _puestos_activos()
    asignaciones = _asignaciones_por_puesto(dia - timedelta(days=1), dia)
    ausencias = _ausencias_de(dia, dia)
    tablero = _tablero_de_dia(dia, puestos, asignaciones, ausencias)

    # La lista lleva a todo el mundo, tenga puesto o no: para doblar un M1 con
    # un T1 hay que poder arrastrar otra vez a quien ya esta colocada. Quien ya
    # tiene algo lo lleva escrito al lado.
    puestos_de = {}
    for nombre, fila in tablero['filas'].items():
        if nombre == 'reflejos':
            continue
        for c in fila:
            if c['ocupa']:
                puestos_de.setdefault(c['ocupa']['cleaner_id'], []).append(c['code'])

    banquillo = []
    for c in _personal_del_tablero():
        tipo = ausencias.get(c.id, {}).get(dia)
        banquillo.append({'cleaner_id': c.id, 'name': c.name, **_pinta(c),
                          'ausente': tipo.name if tipo else None,
                          'puestos': puestos_de.get(c.id, [])})
    sin_puesto = sum(1 for b in banquillo if not b['puestos'] and not b['ausente'])

    return render_template(
        'shift_board_day.html',
        dia=dia, tablero=tablero, banquillo=banquillo, sin_puesto=sin_puesto,
        tipos_del_tablero=ShiftType.query.filter(
            ShiftType.active.is_(True), ShiftType.board_row.isnot(None)
        ).order_by(ShiftType.sort_order, ShiftType.name).all(),
        filas=FILAS_TABLERO, etiquetas_fila=ETIQUETAS_FILA,
        dia_anterior=(dia - timedelta(days=1)).isoformat(),
        dia_siguiente=(dia + timedelta(days=1)).isoformat(),
        semana=_lunes_de(dia).isoformat(),
        hay_puestos=bool(puestos),
    )


def _puestos_del_dia(dia: date) -> dict:
    """{cleaner_id: ['M1', 'T1']} de un dia.

    Lo devuelven asignar y quitar para que la lista de personal no tenga que
    adivinar quien lleva que: desde que se puede doblar, una persona puede estar
    en dos casillas y la pantalla se desincronizaria sola.
    """
    filas = ShiftAssignment.query.options(
        joinedload(ShiftAssignment.position)
    ).filter(ShiftAssignment.date == dia,
             ShiftAssignment.position_id.isnot(None)).all()
    salida: dict = {}
    for a in filas:
        if a.position:
            salida.setdefault(a.cleaner_id, []).append(a.position.code)
    return salida


@bp.route('/cuadrantes/tablero/asignar', methods=['POST'])
@admin_required
def shift_board_assign():
    """Pone a una persona en una casilla. Es lo que hace soltar una ficha."""
    datos = request.get_json(silent=True) or {}
    try:
        dia = date.fromisoformat(datos.get('date'))
        puesto = db.session.get(ShiftPosition, int(datos.get('position_id')))
        trabajadora = db.session.get(Cleaner, int(datos.get('cleaner_id')))
    except (TypeError, ValueError):
        return jsonify({'error': 'Faltan datos o no son validos.'}), 400

    if not puesto or not puesto.active:
        return jsonify({'error': 'Ese puesto ya no existe. Recarga la pagina.'}), 404
    if not trabajadora or not trabajadora.active:
        return jsonify({'error': 'Esa trabajadora ya no esta de alta.'}), 404
    if _esta_ausente(trabajadora.id, dia):
        return jsonify({'error': f'{trabajadora.name} esta de baja o de vacaciones ese dia.'}), 400

    # Quien estuviera en la casilla sale al banquillo. Se borra y se vacia la
    # sesion antes de volver a ocupar el puesto, que si no choca con
    # uq_date_position dentro del mismo commit.
    ocupante = ShiftAssignment.query.filter_by(date=dia, position_id=puesto.id).first()
    if ocupante and ocupante.cleaner_id == trabajadora.id:
        # Ya estaba ahi: soltarla encima de su propia casilla no es nada. Sin
        # esto se intentaria crear una segunda fila para el mismo puesto y dia.
        return jsonify({'ok': True, 'assignment_id': ocupante.id,
                        'desplazada': None, 'puestos': _puestos_del_dia(dia)})

    desplazada = None
    if ocupante:
        desplazada = {'cleaner_id': ocupante.cleaner_id,
                      'name': ocupante.cleaner.name if ocupante.cleaner else '',
                      **(_pinta(ocupante.cleaner) if ocupante.cleaner
                         else {'color': '', 'patron': 'liso'})}
        db.session.delete(ocupante)
        ok, error = _safe_flush('No se pudo liberar la casilla')
        if not ok:
            return jsonify({'error': error}), 500

    # El gesto dice que hacer. Arrastrar una ficha de una casilla a otra la
    # mueve; arrastrarla de la lista de personal anade un puesto mas, porque
    # doblar un M1 con un T1 el mismo dia es algo que pasa cuando falta alguien.
    origen = None
    if datos.get('from_position_id'):
        origen = ShiftAssignment.query.filter_by(
            cleaner_id=trabajadora.id, date=dia,
            position_id=datos.get('from_position_id')).first()

    anterior = origen.position.code if (origen and origen.position) else None
    if origen:
        origen.position_id = puesto.id
        origen.shift_type_id = puesto.shift_type_id
        propia = origen
    else:
        propia = ShiftAssignment(cleaner_id=trabajadora.id, date=dia,
                                 shift_type_id=puesto.shift_type_id,
                                 position_id=puesto.id, created_by=current_user.id)
        db.session.add(propia)
    # Siempre override: lo que se coloca a mano no puede borrarlo la siguiente
    # generacion automatica del mes.
    propia.is_override = True
    propia.source = 'manual'
    propia.updated_at = datetime.now()

    log_audit('update', 'shift_assignment', propia.id or 0,
              {'fecha': dia.isoformat(), 'cleaner_id': trabajadora.id,
               'puesto': puesto.code, 'puesto_anterior': anterior,
               'desplazada': desplazada['cleaner_id'] if desplazada else None})
    ok, error = _safe_commit('No se pudo guardar la asignacion')
    if not ok:
        return jsonify({'error': error}), 500

    return jsonify({'ok': True, 'assignment_id': propia.id, 'desplazada': desplazada,
                    'puestos': _puestos_del_dia(dia)})


@bp.route('/cuadrantes/tablero/quitar', methods=['POST'])
@admin_required
def shift_board_remove():
    """Saca a una persona de una casilla. Si dobla, de esa y solo de esa."""
    datos = request.get_json(silent=True) or {}
    try:
        dia = date.fromisoformat(datos.get('date'))
        cleaner_id = int(datos.get('cleaner_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Faltan datos o no son validos.'}), 400

    consulta = ShiftAssignment.query.filter_by(cleaner_id=cleaner_id, date=dia)
    if datos.get('position_id'):
        consulta = consulta.filter_by(position_id=datos.get('position_id'))
    propia = consulta.first()
    if not propia:
        return jsonify({'ok': True})          # ya no estaba: nada que hacer

    log_audit('delete', 'shift_assignment', propia.id,
              {'fecha': dia.isoformat(), 'cleaner_id': cleaner_id,
               'puesto': propia.position.code if propia.position else None})
    db.session.delete(propia)
    ok, error = _safe_commit('No se pudo quitar la asignacion')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'puestos': _puestos_del_dia(dia)})


def _horario_detalle(cleaner_id: int, lunes: date) -> list:
    """Los siete dias de una persona, listos para pintar o para escribir.

    Devuelve la semana entera, dias libres incluidos: un hueco en la lista se
    lee como un olvido, y un "Libre" escrito no.
    """
    domingo = lunes + timedelta(days=6)
    filas = ShiftAssignment.query.options(
        joinedload(ShiftAssignment.shift_type), joinedload(ShiftAssignment.position)
    ).filter(
        ShiftAssignment.cleaner_id == cleaner_id,
        ShiftAssignment.date >= lunes, ShiftAssignment.date <= domingo,
    ).all()
    por_dia = {}
    for a in filas:
        por_dia.setdefault(a.date, []).append(a)
    ausencias = _ausencias_de(lunes, domingo).get(cleaner_id, {})

    dias = []
    for i in range(7):
        dia = lunes + timedelta(days=i)
        delDia = [a for a in por_dia.get(dia, []) if a.shift_type]
        delDia.sort(key=lambda a: a.shift_type.start_time)
        tipo = ausencias.get(dia)
        dias.append({
            'fecha': dia,
            'dow': DIAS_SEMANA[dia.weekday()],
            'ausencia': tipo.name if tipo else None,
            # La ausencia manda: si esta de baja, lo que diga el cuadrante no
            # es lo que va a hacer esa semana.
            'turnos': [] if tipo else [{
                'name': a.shift_type.name,
                'desde': a.shift_type.start_time.strftime('%H:%M'),
                'hasta': a.shift_type.end_time.strftime('%H:%M'),
                'code': a.position.code if a.position else None,
                'color': a.shift_type.color or DEFAULT_SHIFT_COLOR,
            } for a in delDia],
        })
    return dias


def _horario_de(cleaner_id: int, lunes: date) -> list:
    """Los siete dias de una persona, en texto, para avisarla."""
    lineas = []
    for d in _horario_detalle(cleaner_id, lunes):
        cabecera = f"{d['dow'][:3]} {d['fecha'].strftime('%d/%m')}"
        if d['ausencia']:
            lineas.append(f"{cabecera}: {d['ausencia']}")
        elif d['turnos']:
            trozos = []
            for t in d['turnos']:
                texto = f"{t['name']} ({t['desde']}-{t['hasta']})"
                if t['code']:
                    texto += f" · {t['code']}"
                trozos.append(texto)
            # Quien dobla tiene que verlo escrito, no deducirlo.
            lineas.append(f'{cabecera}: ' + ' + '.join(trozos))
        else:
            lineas.append(f'{cabecera}: Libre')
    return lineas


@bp.route('/cuadrantes/tablero/publicar', methods=['POST'])
@admin_required
def shift_board_publish():
    """Da la semana por buena y avisa a quien le toca.

    Publicar no congela nada: las bajas salen cuando salen y hay que poder
    recolocar. Lo que hace es marcar la foto, y si la semana ya estaba
    publicada avisa solo a quien le haya cambiado algo desde entonces.
    """
    datos = request.get_json(silent=True) or {}
    try:
        lunes = _lunes_de(date.fromisoformat(datos.get('week_start')))
    except (TypeError, ValueError):
        return jsonify({'error': 'Semana no valida.'}), 400

    domingo = lunes + timedelta(days=6)
    pub = ShiftWeekPublication.query.filter_by(week_start=lunes).first()
    desde = pub.published_at if pub else None

    consulta = ShiftAssignment.query.filter(
        ShiftAssignment.date >= lunes, ShiftAssignment.date <= domingo)
    if desde:
        consulta = consulta.filter(
            func.coalesce(ShiftAssignment.updated_at,
                          ShiftAssignment.created_at) > desde)
    destinatarias = sorted({a.cleaner_id for a in consulta.all()})

    ahora = datetime.now()
    if pub:
        pub.published_at = ahora
        pub.published_by = current_user.id
        pub.notified_at = ahora
    else:
        pub = ShiftWeekPublication(week_start=lunes, published_at=ahora,
                                   published_by=current_user.id, notified_at=ahora)
        db.session.add(pub)

    rotulo = f"{lunes.strftime('%d/%m')} al {domingo.strftime('%d/%m')}"
    titulo = 'Horario actualizado' if desde else 'Nuevo horario'
    for cleaner_id in destinatarias:
        db.session.add(Notification(
            type='shift_published', title=titulo,
            message=f'Ya tienes el horario de la semana del {rotulo}.',
            severity='info', link='/worker', worker_id=cleaner_id))

    log_audit('update', 'shift_week_publication', pub.id or 0,
              {'semana': lunes.isoformat(), 'avisadas': len(destinatarias)})
    ok, error = _safe_commit('No se pudo publicar la semana')
    if not ok:
        return jsonify({'error': error}), 500

    # El aviso va despues del commit y nunca lleva el horario dentro: viaja por
    # un servicio externo, igual que el de la mensajeria.
    from .notifications import send_push_to_worker
    for cleaner_id in destinatarias:
        send_push_to_worker(cleaner_id, titulo,
                            f'Semana del {rotulo}. Abre la aplicacion para verlo.',
                            url='/worker', tag=f'horario-{lunes.isoformat()}')

    return jsonify({'ok': True, 'avisadas': len(destinatarias),
                    'published_at': pub.published_at.strftime('%d/%m/%Y %H:%M')})


# ── EL HORARIO POR WHATSAPP ──

WHATSAPP_API = 'https://graph.facebook.com/v21.0'
WHATSAPP_TIMEOUT = 20      # Meta contesta en menos de un segundo; 20 ya es un fallo

# El enlace que recibe cada trabajadora va firmado y lleva dentro de quien es y
# de que semana, asi que no sirve para ver el horario de otra: cambiar un
# caracter invalida la firma. Noventa dias es de sobra para una semana de
# trabajo y evita que un enlace viejo siga abriendo para siempre.
_FIRMA_HORARIO = 'horario-semanal'
_VALIDEZ_ENLACE = 90 * 24 * 3600


def _telefono_e164(texto: str | None, pais: str = '34') -> str | None:
    """El numero como lo quiere WhatsApp: solo digitos y con prefijo de pais.

    La ficha se rellena a mano y ahi aparece de todo: con espacios, con +34,
    con 0034 o a secas. La API solo acepta E.164, y un numero sin prefijo
    genera un wa.me que WhatsApp interpreta mal.
    """
    digitos = re.sub(r'[^0-9]', '', texto or '')
    if not digitos:
        return None
    if digitos.startswith('00'):
        digitos = digitos[2:]
    # Nueve cifras es un movil espanol sin prefijo, que es como se escribe aqui.
    if len(digitos) == 9:
        digitos = pais + digitos
    # Menos de once no es un numero internacional; mas de quince no existe.
    if not 11 <= len(digitos) <= 15:
        return None
    return digitos


def _firma_horario():
    """El firmador de los enlaces.

    Usa SECRET_KEY, que se guarda en instance/.secret_key. Si se pierde esa
    carpeta la clave se regenera y los enlaces ya mandados dejan de valer: hay
    que volver a enviarlos. Mismo aviso que para las claves VAPID.
    """
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.config['SECRET_KEY'], salt=_FIRMA_HORARIO)


def _token_horario(cleaner_id: int, lunes: date) -> str:
    return _firma_horario().dumps({'c': cleaner_id, 's': lunes.isoformat()})


def _leer_token_horario(token: str):
    """(cleaner_id, lunes) si el enlace es bueno, None si no."""
    from itsdangerous import BadData
    try:
        datos = _firma_horario().loads(token, max_age=_VALIDEZ_ENLACE)
        return int(datos['c']), date.fromisoformat(datos['s'])
    except (BadData, KeyError, TypeError, ValueError):
        return None


def _enlace_horario(cleaner_id: int, lunes: date) -> str | None:
    """La direccion completa de su horario, o None si no se puede construir.

    No vale url_for(_external=True): la aplicacion corre detras del proxy del
    NAS y sin ProxyFix saldria con http y el host del contenedor, asi que el
    enlace no abriria en ningun movil. De ahi PUBLIC_BASE_URL.
    """
    base = (app.config.get('PUBLIC_BASE_URL') or '').rstrip('/')
    if not base:
        return None
    return base + url_for('shifts.horario_publico',
                          token=_token_horario(cleaner_id, lunes))


def _whatsapp_configurado() -> bool:
    return bool(app.config.get('WHATSAPP_TOKEN')
                and app.config.get('WHATSAPP_PHONE_ID'))


def _enviar_whatsapp(telefono: str, nombre: str, rotulo: str,
                     enlace: str) -> tuple:
    """Manda la plantilla por la API de Meta. Devuelve (id_del_mensaje, error).

    El mensaje lleva el enlace, no el horario: viaja por un servicio externo,
    igual que el aviso del movil. Y como para confirmar hay que abrir el enlace
    de todas formas, no se pierde nada por el camino.
    """
    import requests
    if not _whatsapp_configurado():
        return None, 'WhatsApp no esta configurado en el servidor.'

    cuerpo = {
        'messaging_product': 'whatsapp',
        'to': telefono,
        'type': 'template',
        'template': {
            'name': app.config.get('WHATSAPP_TEMPLATE') or 'horario_semanal',
            'language': {'code': app.config.get('WHATSAPP_LANG') or 'es'},
            'components': [{
                'type': 'body',
                'parameters': [{'type': 'text', 'text': t}
                               for t in (nombre, rotulo, enlace)],
            }],
        },
    }
    url = f"{WHATSAPP_API}/{app.config['WHATSAPP_PHONE_ID']}/messages"
    try:
        r = requests.post(
            url, json=cuerpo, timeout=WHATSAPP_TIMEOUT,
            headers={'Authorization': f"Bearer {app.config['WHATSAPP_TOKEN']}"})
        datos = r.json() if r.content else {}
        if r.status_code >= 400:
            # El detalle tecnico al registro; a la pantalla, algo que se entienda.
            app.logger.error('WhatsApp %s: %s', r.status_code, r.text[:400])
            motivo = (datos.get('error') or {}).get('message') or ''
            if motivo:
                return None, 'WhatsApp lo ha rechazado: ' + motivo
            return None, 'WhatsApp lo ha rechazado.'
        return ((datos.get('messages') or [{}])[0].get('id') or 'enviado'), None
    except requests.RequestException as e:
        app.logger.error('WhatsApp sin respuesta: %s', e)
        return None, 'No se ha podido conectar con WhatsApp.'
    except ValueError as e:
        app.logger.error('WhatsApp respuesta ilegible: %s', e)
        return None, 'WhatsApp ha contestado algo que no se entiende.'


def _recibos_de(lunes: date) -> dict:
    """Los acuses de una semana, por trabajadora."""
    return {r.cleaner_id: r for r in
            ShiftWeekReceipt.query.filter_by(week_start=lunes).all()}


def _recibo_de(lunes: date, cleaner_id: int) -> ShiftWeekReceipt:
    """El acuse de esa persona y esa semana, creandolo si hace falta."""
    recibo = ShiftWeekReceipt.query.filter_by(
        week_start=lunes, cleaner_id=cleaner_id).first()
    if not recibo:
        recibo = ShiftWeekReceipt(week_start=lunes, cleaner_id=cleaner_id)
        db.session.add(recibo)
    return recibo


def _con_turno_en(lunes: date) -> set:
    """Quien tiene algo asignado esa semana."""
    domingo = lunes + timedelta(days=6)
    return {a.cleaner_id for a in ShiftAssignment.query.filter(
        ShiftAssignment.date >= lunes, ShiftAssignment.date <= domingo).all()}


def _resumen_acuses(lunes: date) -> dict:
    """Cuantas tienen turno, a cuantas se les ha mandado y cuantas lo aceptan.

    Es la pregunta que de verdad se hace quien planifica, y por eso va al lado
    del estado de la publicacion y no escondida en otra pantalla.
    """
    con_turno = _con_turno_en(lunes)
    recibos = _recibos_de(lunes)
    return {
        'total': len(con_turno),
        'enviadas': sum(1 for c in con_turno
                        if recibos.get(c) and recibos[c].sent_at),
        'aceptadas': sum(1 for c in con_turno
                         if recibos.get(c) and recibos[c].accepted_at),
    }


ESTADOS_ACUSE = {
    'pendiente': ('Sin enviar', 'muted'),
    'enviado': ('Enviado', 'info'),
    'visto': ('Visto', 'warning'),
    'aceptado': ('Aceptado', 'success'),
    'error': ('No se pudo enviar', 'danger'),
}


@bp.route('/cuadrantes/tablero/envios')
@admin_required
def shift_board_envios():
    """A quien hay que mandarle el horario, y que ha pasado con cada envio.

    Una fila por trabajadora con turno: su numero tal y como se va a mandar
    -para que un numero mal escrito se vea antes de gastar un mensaje-, en que
    estado esta y los botones. Si Meta no esta configurado queda el wa.me de
    siempre y se manda a mano, asi que esto sirve desde el primer dia.
    """
    from urllib.parse import quote

    lunes = _lunes_de(_dia_o_hoy(request.args.get('semana')))
    domingo = lunes + timedelta(days=6)
    rotulo = f"{lunes.strftime('%d/%m')} al {domingo.strftime('%d/%m/%Y')}"

    con_turno = _con_turno_en(lunes)
    recibos = _recibos_de(lunes)

    filas = []
    for c in _personal_del_tablero():
        if c.id not in con_turno:
            continue
        texto = ('Hola ' + c.name + ', tu horario del ' + rotulo + ':\n\n'
                 + '\n'.join(_horario_de(c.id, lunes)))
        telefono = _telefono_e164(c.phone)
        recibo = recibos.get(c.id)
        estado = recibo.estado if recibo else 'pendiente'
        etiqueta, tono = ESTADOS_ACUSE[estado]
        filas.append({
            'cleaner_id': c.id, 'name': c.name, **_pinta(c),
            'phone': c.phone or '', 'telefono': telefono,
            'estado': estado, 'etiqueta': etiqueta, 'tono': tono,
            'recibo': recibo, 'enlace': _enlace_horario(c.id, lunes),
            'texto': texto,
            'wa_url': ('https://wa.me/' + telefono + '?text=' + quote(texto)
                       if telefono else None),
        })

    return render_template(
        'shift_board_envios.html',
        lunes=lunes, domingo=domingo, rotulo=rotulo, filas=filas,
        semana_anterior=(lunes - timedelta(days=7)).isoformat(),
        semana_siguiente=(lunes + timedelta(days=7)).isoformat(),
        configurado=_whatsapp_configurado(),
        tiene_base=bool(app.config.get('PUBLIC_BASE_URL')),
        resumen={
            'total': len(filas),
            'enviadas': sum(1 for f in filas if f['recibo'] and f['recibo'].sent_at),
            'aceptadas': sum(1 for f in filas if f['recibo'] and f['recibo'].accepted_at),
            'sin_telefono': sum(1 for f in filas if not f['telefono']),
        },
    )


@bp.route('/cuadrantes/tablero/enviar', methods=['POST'])
@admin_required
@limiter.limit('30/minute')
def shift_board_enviar():
    """Manda el horario por WhatsApp a quien se diga.

    Sincrono y por lotes cortos a proposito: aqui hace falta saber cual ha
    fallado para poder repetirla, y un "enviado" que en realidad significa "lo
    he puesto en un hilo" no sirve de nada. Lleva tope de ritmo porque cada
    mensaje cuesta dinero y sale a un tercero.
    """
    datos = request.get_json(silent=True) or {}
    try:
        lunes = _lunes_de(date.fromisoformat(datos.get('week_start')))
    except (TypeError, ValueError):
        return jsonify({'error': 'Semana no valida.'}), 400

    if not _whatsapp_configurado():
        return jsonify({'error': 'WhatsApp no esta configurado. Hay que anadir '
                                 'WHATSAPP_TOKEN y WHATSAPP_PHONE_ID.'}), 400
    if not app.config.get('PUBLIC_BASE_URL'):
        # Sin esto el enlace saldria con el host interno del contenedor y no
        # abriria en ningun movil: mejor no gastar el mensaje.
        return jsonify({'error': 'Falta PUBLIC_BASE_URL: sin ella el enlace del '
                                 'horario no abriria desde fuera.'}), 400

    con_turno = _con_turno_en(lunes)
    pedidas = datos.get('cleaner_ids') or []
    if pedidas:
        objetivo = [c for c in pedidas if c in con_turno]
    else:
        # Sin lista, las que faltan: lo que ya salio no se repite por su cuenta,
        # porque cada mensaje se paga.
        recibos = _recibos_de(lunes)
        objetivo = [c for c in con_turno
                    if not (recibos.get(c) and recibos[c].sent_at)]

    rotulo = (lunes.strftime('%d/%m') + ' al '
              + (lunes + timedelta(days=6)).strftime('%d/%m/%Y'))
    detalle, enviados, fallidos = [], 0, 0

    for cleaner_id in sorted(objetivo):
        c = db.session.get(Cleaner, cleaner_id)
        if not c or not c.active:
            continue
        recibo = _recibo_de(lunes, c.id)
        telefono = _telefono_e164(c.phone)
        if not telefono:
            recibo.error = 'No tiene un telefono valido en su ficha.'
            fallidos += 1
            detalle.append({'cleaner_id': c.id, 'name': c.name, 'ok': False,
                            'error': recibo.error})
            continue

        mensaje_id, error = _enviar_whatsapp(
            telefono, c.name, rotulo, _enlace_horario(c.id, lunes))
        if error:
            recibo.error = error
            fallidos += 1
            detalle.append({'cleaner_id': c.id, 'name': c.name, 'ok': False,
                            'error': error})
        else:
            recibo.sent_at = datetime.now()
            recibo.channel = 'whatsapp'
            recibo.provider_id = mensaje_id
            recibo.error = None
            enviados += 1
            detalle.append({'cleaner_id': c.id, 'name': c.name, 'ok': True})

    log_audit('send', 'shift_week_receipt', 0,
              {'semana': lunes.isoformat(), 'enviados': enviados,
               'fallidos': fallidos})
    ok, error = _safe_commit('No se pudo guardar el resultado del envio')
    if not ok:
        return jsonify({'error': error}), 500

    return jsonify({'ok': True, 'enviados': enviados, 'fallidos': fallidos,
                    'detalle': detalle})


# ── LA PAGINA DE LA TRABAJADORA (enlace firmado, sin sesion) ──
#
# Las dos unicas rutas publicas del proyecto. No llevan @admin_required ni
# @jwt_required() porque quien las abre es una trabajadora desde un enlace de
# WhatsApp, sin sesion y sin tener la aplicacion instalada. Lo que autoriza es
# la firma del token: lleva dentro a quien pertenece y de que semana, caduca a
# los noventa dias, y solo da acceso al horario de esa persona. Ni un dato de
# residentes, que es la linea que no se cruza al abrir algo a internet.
# La excepcion esta escrita en CLAUDE.md y en .claude/rules/04-seguridad.md.

@bp.route('/horario/<token>')
def horario_publico(token):
    """El horario de una persona, para que lo vea y lo confirme."""
    leido = _leer_token_horario(token)
    if not leido:
        return render_template('horario_publico.html', valido=False), 404
    cleaner_id, lunes = leido
    c = db.session.get(Cleaner, cleaner_id)
    if not c or not c.active:
        return render_template('horario_publico.html', valido=False), 404

    recibo = _recibo_de(lunes, cleaner_id)
    if not recibo.opened_at:
        # El "visto" es la primera vez que lo abre; recargar no lo mueve.
        recibo.opened_at = datetime.now()
        if not recibo.channel:
            recibo.channel = 'enlace'
        _safe_commit('No se pudo apuntar que lo has abierto')

    domingo = lunes + timedelta(days=6)
    return render_template(
        'horario_publico.html', valido=True, token=token, trabajadora=c,
        lunes=lunes, domingo=domingo,
        rotulo=lunes.strftime('%d/%m') + ' al ' + domingo.strftime('%d/%m/%Y'),
        dias=_horario_detalle(cleaner_id, lunes),
        recibo=recibo,
    )


@bp.route('/horario/<token>/confirmar', methods=['POST'])
def horario_publico_confirmar(token):
    """Apunta que lo ha visto y que lo da por bueno."""
    leido = _leer_token_horario(token)
    if not leido:
        return render_template('horario_publico.html', valido=False), 404
    cleaner_id, lunes = leido
    c = db.session.get(Cleaner, cleaner_id)
    if not c or not c.active:
        return render_template('horario_publico.html', valido=False), 404

    recibo = _recibo_de(lunes, cleaner_id)
    # Confirmar dos veces no mueve la fecha: vale la primera, que es cuando se
    # entero.
    if not recibo.accepted_at:
        recibo.accepted_at = datetime.now()
        if not recibo.opened_at:
            recibo.opened_at = recibo.accepted_at
        _safe_commit('No se pudo guardar tu confirmacion')

    return redirect(url_for('shifts.horario_publico', token=token))


# ── PUESTOS ──────────────────────────────────────────────────────────────────

@bp.route('/cuadrantes/puestos')
@admin_required
def manage_shift_positions():
    puestos = ShiftPosition.query.options(joinedload(ShiftPosition.shift_type)).join(
        ShiftType).order_by(ShiftType.sort_order, ShiftType.id,
                            ShiftPosition.sort_order, ShiftPosition.id).all()
    return render_template(
        'manage_shift_positions.html',
        puestos=puestos,
        shift_types=ShiftType.query.filter_by(active=True).order_by(
            ShiftType.sort_order, ShiftType.name).all(),
        filas=FILAS_TABLERO, etiquetas_fila=ETIQUETAS_FILA,
    )


def _codigo_libre(shift_type_id: int, codigo: str, excepto_id: int | None = None) -> bool:
    """Si no hay ya otro puesto con ese codigo dentro del mismo turno."""
    return ShiftPosition.query.filter(
        ShiftPosition.shift_type_id == shift_type_id,
        func.lower(ShiftPosition.code) == (codigo or '').lower(),
        ShiftPosition.id != (excepto_id or 0),
    ).first() is None


@bp.route('/cuadrantes/puestos/add_edit', methods=['POST'])
@admin_required
def add_edit_shift_position():
    position_id = request.form.get('position_id', type=int)
    codigo = (request.form.get('code') or '').strip().upper()
    shift_type_id = request.form.get('shift_type_id', type=int)

    if not codigo or not shift_type_id:
        flash('El codigo y el turno son obligatorios.', 'danger')
        return redirect(url_for('shifts.manage_shift_positions'))
    if len(codigo) > 8:
        flash('El codigo no puede pasar de 8 caracteres.', 'danger')
        return redirect(url_for('shifts.manage_shift_positions'))
    if not db.session.get(ShiftType, shift_type_id):
        flash('Ese tipo de turno no existe.', 'danger')
        return redirect(url_for('shifts.manage_shift_positions'))

    if not _codigo_libre(shift_type_id, codigo, position_id):
        flash(f'Ya hay un puesto {codigo} en ese turno.', 'danger')
        return redirect(url_for('shifts.manage_shift_positions'))

    puesto = db.session.get(ShiftPosition, position_id) if position_id else None
    accion = 'update' if puesto else 'create'
    if not puesto:
        puesto = ShiftPosition(shift_type_id=shift_type_id, code=codigo)
        db.session.add(puesto)
    puesto.shift_type_id = shift_type_id
    puesto.code = codigo
    puesto.name = (request.form.get('name') or '').strip() or None
    puesto.sort_order = request.form.get('sort_order', type=int) or 0
    puesto.active = request.form.get('active') == 'on'
    # El reflejo solo tiene sentido en manana y tarde: es donde esta esa persona
    # ademas de en su franja de noche.
    echo = request.form.get('echo_row') or None
    puesto.echo_row = echo if echo in ('manana', 'tarde') else None
    puesto.echo_previous_day = (request.form.get('echo_previous_day') == 'on'
                                and puesto.echo_row is not None)

    log_audit(accion, 'shift_position', puesto.id or 0,
              {'code': codigo, 'shift_type_id': shift_type_id,
               'echo_row': puesto.echo_row})
    ok, error = _safe_commit('No se pudo guardar el puesto')
    flash(error if not ok else 'Puesto guardado.', 'danger' if not ok else 'success')
    return redirect(url_for('shifts.manage_shift_positions'))


@bp.route('/cuadrantes/puestos/delete/<int:position_id>', methods=['POST'])
@admin_required
def delete_shift_position(position_id: int):
    puesto = db.session.get(ShiftPosition, position_id)
    if not puesto:
        flash('Ese puesto ya no existe.', 'warning')
        return redirect(url_for('shifts.manage_shift_positions'))

    # Misma guarda que los tipos de turno: borrar algo que ya se ha usado deja
    # cuadrantes antiguos sin sentido. Para dejar de usarlo esta el desactivar.
    usos = ShiftAssignment.query.filter_by(position_id=puesto.id).count()
    if usos:
        flash(f'No se puede borrar: hay {usos} asignaciones en ese puesto. '
              'Desactivalo si ya no se usa.', 'danger')
        return redirect(url_for('shifts.manage_shift_positions'))

    log_audit('delete', 'shift_position', puesto.id, {'code': puesto.code})
    db.session.delete(puesto)
    ok, error = _safe_commit('No se pudo borrar el puesto')
    flash(error if not ok else 'Puesto eliminado.', 'danger' if not ok else 'success')
    return redirect(url_for('shifts.manage_shift_positions'))


# ── EL CUADRANTE DE SIEMPRE, MONTADO DE UNA VEZ ─────────────────────────────
# Dar de alta a mano nueve turnos y quince puestos antes de poder planificar
# nada es una barrera tonta: el reparto de la residencia es el del cuadrante de
# papel y lleva anos siendo el mismo. Esto lo deja puesto y luego se retoca.
#
# Los horarios de la manana, la tarde y el refuerzo son los de la leyenda del
# cuadrante; los de las noches y los puestos de fuera son una propuesta, y se
# ajustan en Tipos de turno. Lo que no se adivina son las horas; la forma del
# recuadro si.

CUADRANTE_BASE = [
    # nombre, abrev, color, desde, hasta, fila,
    #   [(codigo, descripcion, se_refleja_en, el_reflejo_es_de_ayer)]
    ('Mañana', 'M', '#cfe2ff', dt_time(7, 30), dt_time(14, 0), 'manana', [
        ('M1', 'Primera de mañana', None, False),
        ('M2', 'Segunda de mañana', None, False),
        ('M3', 'Tercera de mañana', None, False),
        ('CRM', 'Coordinadora de mañana', None, False),
    ]),
    ('Refuerzo de mañana', 'RFM', '#ffe8a3', dt_time(7, 30), dt_time(9, 30), 'manana', [
        ('RFM', 'Refuerzo de mañana', None, False),
    ]),
    ('Tarde', 'T', '#d8f5d0', dt_time(14, 0), dt_time(20, 30), 'tarde', [
        ('T1', 'Primera de tarde', None, False),
        ('T2', 'Segunda de tarde', None, False),
        ('T3', 'Tercera de tarde', None, False),
        ('CRT', 'Coordinadora de tarde', None, False),
    ]),
    ('Refuerzo de tarde', 'RFT', '#ffd9c4', dt_time(14, 0), dt_time(16, 0), 'tarde', [
        ('RFT', 'Refuerzo de tarde', None, False),
    ]),
    # Entra por la tarde y acaba de noche, asi que esta toda la tarde en la casa.
    ('Noche A', 'NA', '#1f6fb5', dt_time(15, 0), dt_time(22, 0), 'noche', [
        ('NIT A', 'Primera de noche', 'tarde', False),
    ]),
    ('Noche B', 'NB', '#b6b1d8', dt_time(21, 0), dt_time(8, 0), 'noche', [
        ('NIT B', 'Segunda de noche', None, False),
    ]),
    # Sale por la manana, asi que el dia siguiente la ensena media manana mas.
    ('Noche C', 'NC', '#e2721f', dt_time(21, 0), dt_time(8, 0), 'noche', [
        ('NIT C', 'Tercera de noche', 'manana', True),
    ]),
    ('Cocina', 'CO', '#f7c9d9', dt_time(8, 0), dt_time(16, 0), 'lateral', [
        ('COCINA', 'Cocina', None, False),
    ]),
    ('Recepción', 'RE', '#c4e7f0', dt_time(9, 0), dt_time(14, 0), 'lateral', [
        ('RECEP', 'Recepción', None, False),
    ]),
]


def _sin_tildes(texto: str) -> str:
    """Para comparar nombres de turno escritos de cualquier manera.

    Una residencia que ya tenga su turno de «Manana» no puede acabar con otro
    de «Mañana» al lado por una tilde.
    """
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', (texto or '').strip().lower())
                   if unicodedata.category(c) != 'Mn')


@bp.route('/cuadrantes/puestos/crear-base', methods=['POST'])
@admin_required
def crear_cuadrante_base():
    """Deja montado el reparto del cuadrante de papel: turnos y puestos.

    No pisa nada de lo que ya haya. Un turno que ya existe con ese nombre se
    reutiliza tal cual —sus horas son las que haya puesto alguien, no las de
    aqui— y solo se le rellena la fila del tablero si estaba vacia. Un puesto
    que ya existe se deja como esta. Asi se puede pulsar dos veces sin miedo.
    """
    turnos_nuevos = puestos_nuevos = 0

    for orden_turno, (nombre, abrev, color, desde, hasta, fila, puestos) in enumerate(CUADRANTE_BASE):
        st = next((x for x in ShiftType.query.all()
                   if _sin_tildes(x.name) == _sin_tildes(nombre)), None)
        if not st:
            st = ShiftType(name=nombre, short_name=abrev, color=color,
                           start_time=desde, end_time=hasta, breaks_minutes=0,
                           sort_order=orden_turno, active=True)
            db.session.add(st)
            turnos_nuevos += 1
        if not st.board_row:
            st.board_row = fila
        ok, error = _safe_flush('No se pudo crear el tipo de turno')
        if not ok:
            flash(error, 'danger')
            return redirect(url_for('shifts.manage_shift_positions'))

        for orden, (codigo, descripcion, reflejo, de_ayer) in enumerate(puestos):
            existe = ShiftPosition.query.filter(
                ShiftPosition.shift_type_id == st.id,
                func.lower(ShiftPosition.code) == codigo.lower()).first()
            if existe:
                continue
            db.session.add(ShiftPosition(
                shift_type_id=st.id, code=codigo, name=descripcion,
                sort_order=orden, active=True,
                echo_row=reflejo, echo_previous_day=de_ayer))
            puestos_nuevos += 1

    log_audit('create', 'shift_position', 0,
              {'origen': 'cuadrante base', 'turnos': turnos_nuevos,
               'puestos': puestos_nuevos})
    ok, error = _safe_commit('No se pudo crear el cuadrante base')
    if not ok:
        flash(error, 'danger')
    elif not puestos_nuevos and not turnos_nuevos:
        flash('Ya estaba todo creado: no se ha anadido nada.', 'info')
    else:
        flash(f'Creados {puestos_nuevos} puestos y {turnos_nuevos} tipos de turno. '
              'Revisa los horarios en Tipos de turno: los de las noches y los puestos '
              'de fuera son una propuesta.', 'success')
    return redirect(url_for('shifts.manage_shift_positions'))

@bp.route('/cuadrantes/tablero/dia/<iso>/puesto', methods=['POST'])
@admin_required
def shift_board_add_position(iso: str):
    """Anade un puesto sin salir del recuadro.

    Anadir un M4 o una recepcion de tarde es algo que se piensa mirando el
    cuadrante, no en otra pantalla: desde aqui se ve el hueco. Si el turno al
    que pertenece todavia no existe —una recepcion de tarde no tiene las horas
    de la de manana— se crea de paso.
    """
    dia = _dia_o_hoy(iso)
    volver = redirect(url_for('shifts.shift_board_day', iso=dia.isoformat()))

    codigo = (request.form.get('code') or '').strip().upper()
    if not codigo:
        flash('Ponle un codigo al puesto, como M4 o RECEP T.', 'danger')
        return volver
    if len(codigo) > 8:
        flash('El codigo no puede pasar de 8 caracteres.', 'danger')
        return volver

    if request.form.get('shift_type_id') == 'nuevo':
        nombre = (request.form.get('nuevo_nombre') or '').strip()
        desde = _parse_hhmm(request.form.get('nuevo_desde'))
        hasta = _parse_hhmm(request.form.get('nuevo_hasta'))
        fila = request.form.get('nuevo_board_row')
        if not nombre or not desde or not hasta:
            flash('Al turno nuevo le falta el nombre o las horas.', 'danger')
            return volver
        if desde == hasta:
            flash('La hora de fin no puede ser igual a la de inicio.', 'danger')
            return volver
        if fila not in FILAS_TABLERO:
            flash('Elige en que fila del recuadro va el turno nuevo.', 'danger')
            return volver
        if next((x for x in ShiftType.query.all()
                 if _sin_tildes(x.name) == _sin_tildes(nombre)), None):
            flash(f'Ya existe un turno que se llama {nombre}.', 'danger')
            return volver
        color = request.form.get('nuevo_color') or DEFAULT_SHIFT_COLOR
        if not _HEX_COLOR_RE.match(color):
            color = DEFAULT_SHIFT_COLOR
        st = ShiftType(
            name=nombre,
            short_name=(request.form.get('nuevo_short') or codigo)[:5],
            color=color, start_time=desde, end_time=hasta, breaks_minutes=0,
            sort_order=(db.session.query(func.max(ShiftType.sort_order)).scalar() or 0) + 1,
            active=True, board_row=fila)
        db.session.add(st)
        ok, error = _safe_flush('No se pudo crear el tipo de turno')
        if not ok:
            flash(error, 'danger')
            return volver
    else:
        st = db.session.get(ShiftType, request.form.get('shift_type_id', type=int) or 0)
        if not st:
            flash('Ese tipo de turno no existe.', 'danger')
            return volver

    if not _codigo_libre(st.id, codigo):
        flash(f'Ya hay un puesto {codigo} en el turno de {st.name}.', 'danger')
        return volver

    # Al final de su fila, que es donde se espera que salga lo que se acaba de
    # anadir; el orden se retoca despues si hace falta.
    orden = max((p.sort_order for p in st.positions), default=-1) + 1
    puesto = ShiftPosition(shift_type_id=st.id, code=codigo,
                           name=(request.form.get('name') or '').strip() or None,
                           sort_order=orden, active=True)
    db.session.add(puesto)
    log_audit('create', 'shift_position', 0,
              {'code': codigo, 'shift_type': st.name, 'origen': 'tablero'})
    ok, error = _safe_commit('No se pudo crear el puesto')
    flash(error if not ok else f'Puesto {codigo} anadido a {st.name}.',
          'danger' if not ok else 'success')
    return volver


@bp.route('/cuadrantes/tablero/mes')
@admin_required
def shift_board_month():
    """El mes entero, un recuadro por dia.

    Es el mismo dibujo que la semana a otra escala. Publicar y los envios se
    siguen haciendo por semana —es la unidad en la que se reparte el trabajo—,
    asi que cada fila lleva su estado y su boton y no hace falta salir de aqui.
    """
    mes = request.args.get('mes', '')
    try:
        anyo, numero = (int(x) for x in mes.split('-'))
        primero = date(anyo, numero, 1)
    except (ValueError, TypeError):
        hoy = date.today()
        primero, anyo, numero = hoy.replace(day=1), hoy.year, hoy.month

    import calendar as _cal
    ultimo = date(anyo, numero, _cal.monthrange(anyo, numero)[1])
    # El calendario empieza en lunes y acaba en domingo aunque el mes no: una
    # semana partida por la mitad no se puede publicar ni leer.
    desde = _lunes_de(primero)
    hasta = _lunes_de(ultimo) + timedelta(days=6)

    dias = _tablero_de_rango(desde, hasta)
    semanas = []
    for i in range(0, len(dias), 7):
        tramo = dias[i:i + 7]
        lunes = tramo[0]['fecha']
        pub, modificada = _estado_semana(lunes)
        semanas.append({'lunes': lunes, 'dias': tramo,
                        'publicacion': pub, 'modificada': modificada,
                        'acuses': _resumen_acuses(lunes)})

    # La leyenda, solo con quien trabaja dentro del mes.
    del_mes = [d for d in dias if primero <= d['fecha'] <= ultimo]
    anterior = (primero - timedelta(days=1)).replace(day=1)
    siguiente = (ultimo + timedelta(days=1))

    return render_template(
        'shift_board_month.html',
        primero=primero, ultimo=ultimo, semanas=semanas, hoy=date.today(),
        titulo=f'{MESES[numero - 1]} {anyo}',
        leyenda=_leyenda_de(del_mes),
        mes_anterior=f'{anterior.year}-{anterior.month:02d}',
        mes_siguiente=f'{siguiente.year}-{siguiente.month:02d}',
        hay_puestos=bool(_puestos_activos()),
    )
