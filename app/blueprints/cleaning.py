"""Cleaning routes blueprint — intelligent cleaning plan, analytics & config."""
from __future__ import annotations
from datetime import datetime, timedelta, date

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_
from sqlalchemy.orm import joinedload

from .. import db
from ..models import (
    Room, Cleaner, CleaningRecord, Resident,
    CleaningZoneAssignment, CleaningTargetTime,
    ShiftAssignment, Absence, RoomType, Floor, CareRecord,
)
from ..utils import (
    admin_required, _verify_worker_id, _safe_commit,
    _compute_cleaning_stats, _calculate_room_urgency, _urgency_priority,
)

bp = Blueprint('cleaning', __name__)


# ── RUTAS DE LIMPIEZA INTELIGENTES ──────────────────────────────────────────

@bp.route('/api/worker/cleaning-route')
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

    # Check which numeric rooms have active residents assigned
    occupied_room_numbers = {r.room_number for r in Resident.query.filter_by(active=True).all()
                             if r.room_number}

    # Build room info with estimated times and urgency
    route = []
    for room in rooms:
        est_minutes = avg_per_room.get(room.id, targets.get(room.room_type_id, 15))
        cleaned = room.id in today_cleaned
        floor = room.floor

        # Check if room is a resident room (by type, not by number format)
        is_resident_room = room.room_type and 'residen' in room.room_type.name.lower()
        is_occupied = room.number in occupied_room_numbers

        urgency, days_since, expected_freq_days = _calculate_room_urgency(
            room.id, room_clean_count, room_last_cleaned, now, is_resident_room, is_occupied)
        priority = _urgency_priority(urgency, cleaned)

        # Priority label
        empty_tag = ' (vacía)' if is_resident_room and not is_occupied else ''
        if cleaned:
            priority_label = 'Limpiada hoy'
        elif urgency >= 2:
            priority_label = f'Atrasada ({days_since}d sin limpiar, se limpia cada {expected_freq_days:.0f}d){empty_tag}'
        elif urgency >= 1:
            priority_label = f'Toca hoy ({days_since}d sin limpiar){empty_tag}'
        else:
            remaining_days = expected_freq_days - (days_since or 0)
            priority_label = f'Faltan {remaining_days:.0f}d{empty_tag}'

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

    # Calculate shift duration to fill the full workday
    shift_net_minutes = 0
    if today_shift and today_shift.shift_type:
        st = today_shift.shift_type
        start_dt = datetime.combine(date.today(), st.start_time)
        end_dt = datetime.combine(date.today(), st.end_time)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        shift_net_minutes = ((end_dt - start_dt).total_seconds() / 60) - (st.breaks_minutes or 0)

    remaining_estimated = sum(r['estimated_minutes'] for r in route if not r['cleaned_today'])

    # Only fill shift with extras for 'limpieza' role workers.
    # 'mixto' workers use their spare time for care tasks, not extra cleaning.
    if worker.role != 'limpieza':
        shift_net_minutes = 0  # skip extra room assignment

    # If worker has spare time, assign extra rooms from uncovered pool
    route_room_ids = {r['id'] for r in route}
    if shift_net_minutes > 0 and remaining_estimated < shift_net_minutes:
        spare_minutes = shift_net_minutes - remaining_estimated

        # Rooms in other workers' zones (deprioritized, not excluded)
        other_zone_floor_ids = {za.floor_id for za in CleaningZoneAssignment.query.filter(
            CleaningZoneAssignment.cleaner_id != worker.id,
        ).all()}
        other_zone_room_ids = {r.id for r in Room.query.filter(
            Room.floor_id.in_(other_zone_floor_ids),
        ).all()} if other_zone_floor_ids else set()

        extra_rooms = Room.query.options(
            joinedload(Room.floor), joinedload(Room.room_type)
        ).filter(~Room.id.in_(route_room_ids)).all()

        extra_candidates = []
        for room in extra_rooms:
            if room.id in today_cleaned:
                continue
            est = avg_per_room.get(room.id, targets.get(room.room_type_id, 15))
            is_res = room.room_type and 'residen' in room.room_type.name.lower()
            is_occ = room.number in occupied_room_numbers
            urg, days_since, freq = _calculate_room_urgency(
                room.id, room_clean_count, room_last_cleaned, now, is_res, is_occ)
            in_other_zone = room.id in other_zone_room_ids
            extra_candidates.append((room, est, urg, days_since, freq, in_other_zone))

        # Uncovered rooms first (not in other workers' zones), then by urgency
        extra_candidates.sort(key=lambda x: (x[5], -x[2]))

        for room, est, urg, days_since, freq, _in_other in extra_candidates:
            if spare_minutes <= 0:
                break
            floor = room.floor
            priority = _urgency_priority(urg)
            empty_tag = ''
            is_res = room.room_type and 'residen' in room.room_type.name.lower()
            if is_res and room.number not in occupied_room_numbers:
                empty_tag = ' (vacía)'
            p_label = f'Extra: {days_since}d sin limpiar{empty_tag}' if days_since else f'Extra{empty_tag}'

            route.append({
                'id': room.id, 'number': room.number,
                'description': room.description or '',
                'room_type': room.room_type.name if room.room_type else '',
                'floor_name': floor.name if floor else '',
                'floor_id': floor.id if floor else 0,
                'estimated_minutes': round(est, 1),
                'cleaned_today': False,
                'days_since_cleaned': days_since,
                'expected_frequency': round(freq, 1),
                'urgency': round(urg, 2),
                'priority': priority,
                'priority_label': p_label,
            })
            spare_minutes -= est
            route_room_ids.add(room.id)

        # Re-sort after adding extras
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


@bp.route('/admin/cleaning-plan')
@admin_required
def admin_cleaning_plan():
    """Show today's cleaning plan: who cleans what, when, and progress."""
    target_date = request.args.get('date', '')
    if target_date:
        plan_date = date.fromisoformat(target_date)
    else:
        plan_date = date.today()

    # Get cleaning workers with shift today
    cleaning_workers = Cleaner.query.filter(
        Cleaner.active == True,
        Cleaner.role.in_(['limpieza', 'mixto']),
    ).order_by(Cleaner.name).all()

    shifts_today = {sa.cleaner_id: sa for sa in ShiftAssignment.query.filter_by(date=plan_date).all()}

    # Absences today
    absent_ids = {a.cleaner_id for a in Absence.query.filter(
        Absence.start_date <= plan_date, Absence.end_date >= plan_date,
    ).all()}

    # Today's completed cleanings
    day_start = datetime.combine(plan_date, datetime.min.time())
    day_end = datetime.combine(plan_date + timedelta(days=1), datetime.min.time())
    today_records = CleaningRecord.query.filter(
        CleaningRecord.start_time >= day_start,
        CleaningRecord.start_time < day_end,
    ).options(joinedload(CleaningRecord.cleaner)).all()

    # Group completed by worker
    worker_completed = {}
    for rec in today_records:
        wid = rec.cleaner_id
        worker_completed.setdefault(wid, []).append(rec)

    # Historical stats for route calculation
    stats = _compute_cleaning_stats(90)
    avg_per_room = stats['avg_per_room']
    targets_map = {t.room_type_id: t.target_minutes for t in CleaningTargetTime.query.all()}

    # Room data
    all_rooms = Room.query.options(joinedload(Room.floor), joinedload(Room.room_type)).all()
    room_map = {r.id: r for r in all_rooms}

    # Cleaning frequency data
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

    # Occupied rooms
    occupied = {r.room_number for r in Resident.query.filter_by(active=True).all() if r.room_number}
    now = datetime.now()

    # Build plan per worker
    worker_plans = []
    globally_assigned_extra = set()  # track extras to avoid double-assigning
    for w in cleaning_workers:
        sa = shifts_today.get(w.id)
        is_absent = w.id in absent_ids

        if is_absent:
            worker_plans.append({'worker': w, 'status': 'absent', 'shift': None, 'rooms': [], 'completed': []})
            continue
        if not sa or not sa.shift_type_id:
            worker_plans.append({'worker': w, 'status': 'no_shift', 'shift': None, 'rooms': [], 'completed': []})
            continue

        # Get worker's rooms (auto-detect from history)
        worker_room_ids = {r[0] for r in db.session.query(CleaningRecord.room_id).filter(
            CleaningRecord.cleaner_id == w.id, CleaningRecord.end_time.isnot(None),
            CleaningRecord.start_time >= cutoff_freq,
        ).distinct().all()}

        # Manual zone assignments override
        zone_assigns = CleaningZoneAssignment.query.filter_by(cleaner_id=w.id).all()
        if zone_assigns:
            assigned_floors = {za.floor_id for za in zone_assigns}
            worker_rooms = [r for r in all_rooms if r.floor_id in assigned_floors]
        elif worker_room_ids:
            worker_rooms = [r for r in all_rooms if r.id in worker_room_ids]
        else:
            worker_rooms = all_rooms

        # Today's cleaned room ids for this worker
        completed_room_ids = {rec.room_id for rec in worker_completed.get(w.id, [])}

        # Build room list with urgency
        rooms_plan = []
        for room in worker_rooms:
            is_resident = room.room_type and 'residen' in room.room_type.name.lower()
            is_occ = room.number in occupied
            urgency, days_since, freq = _calculate_room_urgency(
                room.id, room_clean_count, room_last_cleaned, now, is_resident, is_occ)
            priority = _urgency_priority(urgency, room.id in completed_room_ids)

            est = avg_per_room.get(room.id, targets_map.get(room.room_type_id, 15))
            rooms_plan.append({
                'room': room, 'priority': priority, 'urgency': urgency,
                'days_since': days_since, 'frequency': round(freq, 1),
                'estimated_min': round(est, 1),
                'completed': room.id in completed_room_ids,
            })

        rooms_plan.sort(key=lambda r: ({'urgent': 0, 'due': 1, 'ok': 2, 'done': 3}.get(r['priority'], 2), -r['urgency']))

        # Calculate shift net minutes
        shift_net_min = 0
        if sa and sa.shift_type:
            st = sa.shift_type
            start_dt = datetime.combine(plan_date, st.start_time)
            end_dt = datetime.combine(plan_date, st.end_time)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            shift_net_min = round(((end_dt - start_dt).total_seconds() / 60) - (st.breaks_minutes or 0))

        # Fill shift with extra rooms for 'limpieza' workers
        remaining_est = sum(r['estimated_min'] for r in rooms_plan if not r['completed'])
        worker_room_id_set = {r['room'].id for r in rooms_plan}

        if w.role == 'limpieza' and shift_net_min > 0 and remaining_est < shift_net_min:
            spare = shift_net_min - remaining_est
            today_cleaned_ids_all = {rec.room_id for rec in today_records if rec.end_time is not None}
            extra_candidates = []
            for room in all_rooms:
                if room.id in worker_room_id_set or room.id in completed_room_ids:
                    continue
                if room.id in today_cleaned_ids_all or room.id in globally_assigned_extra:
                    continue
                est = avg_per_room.get(room.id, targets_map.get(room.room_type_id, 15))
                is_res = room.room_type and 'residen' in room.room_type.name.lower()
                is_occ = room.number in occupied
                urg, days_since, freq = _calculate_room_urgency(
                    room.id, room_clean_count, room_last_cleaned, now, is_res, is_occ)
                extra_candidates.append((room, est, urg, days_since, freq))

            extra_candidates.sort(key=lambda x: -x[2])  # most urgent first

            for room, est, urg, days_since, freq in extra_candidates:
                if spare <= 0:
                    break
                prio = _urgency_priority(urg)
                rooms_plan.append({
                    'room': room, 'priority': prio, 'urgency': urg,
                    'days_since': days_since, 'frequency': round(freq, 1),
                    'estimated_min': round(est, 1),
                    'completed': False, 'extra': True,
                })
                spare -= est
                worker_room_id_set.add(room.id)
                globally_assigned_extra.add(room.id)

            rooms_plan.sort(key=lambda r: ({'urgent': 0, 'due': 1, 'ok': 2, 'done': 3}.get(r['priority'], 2), -r['urgency']))
            remaining_est = sum(r['estimated_min'] for r in rooms_plan if not r['completed'])

        completed_records = sorted(worker_completed.get(w.id, []), key=lambda r: r.start_time)

        worker_plans.append({
            'worker': w,
            'status': 'working',
            'shift': sa.shift_type if sa else None,
            'rooms': rooms_plan,
            'completed': completed_records,
            'total_rooms': len(rooms_plan),
            'done_count': sum(1 for r in rooms_plan if r['completed']),
            'urgent_count': sum(1 for r in rooms_plan if r['priority'] == 'urgent'),
            'total_est_min': round(sum(r['estimated_min'] for r in rooms_plan)),
            'remaining_est_min': round(remaining_est),
            'shift_net_min': shift_net_min,
        })

    # Find uncovered rooms: rooms that need cleaning today but no worker has them
    covered_room_ids = set()
    for wp in worker_plans:
        if wp['status'] == 'working':
            for rp in wp['rooms']:
                covered_room_ids.add(rp['room'].id)

    today_cleaned_ids = {rec.room_id for rec in today_records}
    uncovered = []
    for room in all_rooms:
        if room.id in covered_room_ids or room.id in today_cleaned_ids:
            continue
        is_resident = room.room_type and 'residen' in room.room_type.name.lower()
        is_occ = room.number in occupied
        urgency, days_since, freq = _calculate_room_urgency(
            room.id, room_clean_count, room_last_cleaned, now, is_resident, is_occ)
        if urgency >= 1:
            uncovered.append({
                'room': room, 'days_since': days_since, 'frequency': round(freq, 1),
                'urgency': round(urgency, 2),
                'priority': _urgency_priority(urgency),
            })
    uncovered.sort(key=lambda x: -x['urgency'])

    return render_template('admin_cleaning_plan.html',
        plan_date=plan_date, worker_plans=worker_plans,
        uncovered=uncovered,
        is_today=plan_date == date.today(),
        prev_date=(plan_date - timedelta(days=1)).isoformat(),
        next_date=(plan_date + timedelta(days=1)).isoformat(),
    )


@bp.route('/admin/cleaning-analytics')
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


@bp.route('/admin/cleaning-config')
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


@bp.route('/admin/cleaning-config/save-assignments', methods=['POST'])
@admin_required
def save_cleaning_assignments():
    cleaner_id = request.form.get('cleaner_id', type=int)
    if not cleaner_id:
        flash('Selecciona un trabajador.', 'danger')
        return redirect(url_for('cleaning.admin_cleaning_config'))
    floor_ids = request.form.getlist('floor_ids')
    CleaningZoneAssignment.query.filter_by(cleaner_id=cleaner_id).delete()
    for fid in floor_ids:
        db.session.add(CleaningZoneAssignment(cleaner_id=cleaner_id, floor_id=int(fid)))
    db.session.commit()
    flash('Zonas asignadas correctamente.', 'success')
    return redirect(url_for('cleaning.admin_cleaning_config'))


@bp.route('/admin/cleaning-config/save-targets', methods=['POST'])
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
    return redirect(url_for('cleaning.admin_cleaning_config'))
