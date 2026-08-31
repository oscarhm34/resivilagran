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
        else:
            existing = ShiftAssignment(
                cleaner_id=cleaner_id, date=target_date,
                shift_type_id=st.id, source='manual',
                created_by=current_user.id,
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
    return render_template('manage_shift_types.html', shift_types=shift_types)


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

    log_audit('create' if is_new else 'update', 'shift_type', st.id, {
        'name': name, 'short_name': short_name,
        'start_time': start_t.strftime('%H:%M'),
        'end_time': end_t.strftime('%H:%M'),
        'breaks_minutes': breaks_min, 'sort_order': sort_order,
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
