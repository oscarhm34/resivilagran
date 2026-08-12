"""Activities calendar — scheduling and participation tracking."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, date

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_login import current_user
from sqlalchemy.orm import joinedload
from sqlalchemy import func, case

from flask_jwt_extended import jwt_required, get_jwt_identity

from .. import db
from ..models import Activity, ActivityParticipation, ActivityTemplate, Resident, Cleaner
from ..utils import admin_required

bp = Blueprint('activities', __name__)

CATEGORIES = {
    'cognitiva': 'Estimulacion cognitiva',
    'fisica': 'Actividad fisica',
    'social': 'Actividad social',
    'creativa': 'Taller creativo',
    'musical': 'Musica / canto',
    'relajacion': 'Relajacion',
    'salida': 'Salida / excursion',
    'general': 'General',
}
CAT_ICONS = {
    'cognitiva': 'bi-puzzle', 'fisica': 'bi-heart-pulse', 'social': 'bi-people',
    'creativa': 'bi-palette', 'musical': 'bi-music-note-beamed', 'relajacion': 'bi-cloud',
    'salida': 'bi-sun', 'general': 'bi-calendar-event',
}
CAT_COLORS = {
    'cognitiva': '#6f42c1', 'fisica': '#dc3545', 'social': '#0d6efd',
    'creativa': '#fd7e14', 'musical': '#d63384', 'relajacion': '#20c997',
    'salida': '#ffc107', 'general': '#6c757d',
}
ENGAGEMENT_LABELS = {
    'participated': 'Ha participat', 'passive': 'Passiu/observador',
    'refused': 'Ha refusat', 'absent': 'Absent',
}


@bp.route('/admin/activities')
@admin_required
def admin_activities():
    """Activity calendar view."""
    target = request.args.get('date', '')
    view_date = date.fromisoformat(target) if target else date.today()

    # Week view: Monday to Sunday
    monday = view_date - timedelta(days=view_date.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]

    activities = Activity.query.filter(
        Activity.activity_date >= days[0], Activity.activity_date <= days[-1],
    ).options(joinedload(Activity.participations)).order_by(
        Activity.activity_date, Activity.start_time,
    ).all()

    by_day = {d: [] for d in days}
    for a in activities:
        if a.activity_date in by_day:
            by_day[a.activity_date].append(a)

    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()
    templates = ActivityTemplate.query.filter_by(active=True).order_by(ActivityTemplate.title).all()

    return render_template('admin_activities.html',
        days=days, by_day=by_day, view_date=view_date,
        monday=monday, categories=CATEGORIES, cat_icons=CAT_ICONS, cat_colors=CAT_COLORS,
        engagement_labels=ENGAGEMENT_LABELS, residents=residents, templates=templates,
        prev_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        is_current_week=date.today() in days,
    )


@bp.route('/admin/activities/save', methods=['POST'])
@admin_required
def save_activity():
    act_id = request.form.get('activity_id', type=int)
    recurring = request.form.get('recurring') == 'on'
    recurrence = request.form.get('recurrence', 'weekly')

    if act_id:
        a = db.session.get(Activity, act_id)
        if not a:
            flash('Activitat no trobada.', 'danger')
            return redirect(url_for('activities.admin_activities'))
    else:
        a = Activity(created_by=current_user.id)
        db.session.add(a)

    a.title = request.form.get('title', '').strip()
    a.description = request.form.get('description', '').strip() or None
    a.activity_date = date.fromisoformat(request.form.get('activity_date'))
    st = request.form.get('start_time', '').strip()
    et = request.form.get('end_time', '').strip()
    a.start_time = datetime.strptime(st, '%H:%M').time() if st else None
    a.end_time = datetime.strptime(et, '%H:%M').time() if et else None
    a.location = request.form.get('location', '').strip() or None
    a.category = request.form.get('category', 'general')

    db.session.commit()

    # Create recurring template + future instances if requested (only for new activities)
    if recurring and not act_id:
        tmpl = ActivityTemplate(
            title=a.title, description=a.description,
            weekday=a.activity_date.weekday(),
            start_time=a.start_time, end_time=a.end_time,
            location=a.location, category=a.category,
            recurrence=recurrence,
            created_by=current_user.id,
        )
        db.session.add(tmpl)
        db.session.flush()
        a.template_id = tmpl.id

        # Generate future instances (8 weeks for weekly, 4 for biweekly, 3 for monthly)
        if recurrence == 'weekly':
            delta, count = timedelta(weeks=1), 8
        elif recurrence == 'biweekly':
            delta, count = timedelta(weeks=2), 4
        else:  # monthly
            delta, count = timedelta(weeks=4), 3

        current_date = a.activity_date
        for _ in range(count):
            current_date += delta
            inst = Activity(
                title=a.title, description=a.description,
                activity_date=current_date,
                start_time=a.start_time, end_time=a.end_time,
                location=a.location, category=a.category,
                template_id=tmpl.id, created_by=current_user.id,
            )
            db.session.add(inst)

        db.session.commit()
        flash(f'Activitat recurrent creada: {a.title} ({count + 1} instancies)', 'success')
    else:
        flash(f'Activitat {"actualitzada" if act_id else "creada"}: {a.title}', 'success')

    return redirect(url_for('activities.admin_activities', date=a.activity_date.isoformat()))


@bp.route('/admin/activities/<int:act_id>/delete', methods=['POST'])
@admin_required
def delete_activity(act_id: int):
    a = db.session.get(Activity, act_id)
    if a:
        act_date = a.activity_date.isoformat()
        scope = request.form.get('scope', 'single')  # single or future
        if scope == 'future' and a.template_id:
            # Delete this and all future instances from the same template
            Activity.query.filter(
                Activity.template_id == a.template_id,
                Activity.activity_date >= a.activity_date,
            ).delete()
            # If no instances left, deactivate template
            remaining = Activity.query.filter_by(template_id=a.template_id).count()
            if remaining == 0:
                tmpl = db.session.get(ActivityTemplate, a.template_id)
                if tmpl:
                    tmpl.active = False
            db.session.commit()
            flash('Activitat i futures instancies eliminades.', 'success')
        else:
            db.session.delete(a)
            db.session.commit()
            flash('Activitat eliminada.', 'success')
        return redirect(url_for('activities.admin_activities', date=act_date))
    return redirect(url_for('activities.admin_activities'))


@bp.route('/admin/activities/templates/<int:tmpl_id>/generate', methods=['POST'])
@admin_required
def generate_template_instances(tmpl_id: int):
    """Generate more future instances from a recurring template."""
    tmpl = db.session.get(ActivityTemplate, tmpl_id)
    if not tmpl:
        return jsonify({'error': 'Plantilla no trobada'}), 404

    weeks = request.form.get('weeks', 4, type=int)

    # Find the last existing instance date
    last = Activity.query.filter_by(template_id=tmpl_id).order_by(
        Activity.activity_date.desc()).first()
    start_date = last.activity_date if last else date.today()

    if tmpl.recurrence == 'weekly':
        delta = timedelta(weeks=1)
    elif tmpl.recurrence == 'biweekly':
        delta = timedelta(weeks=2)
    else:
        delta = timedelta(weeks=4)

    current_date = start_date
    created = 0
    for _ in range(weeks):
        current_date += delta
        # Skip if instance already exists on that date
        exists = Activity.query.filter_by(
            template_id=tmpl_id, activity_date=current_date).first()
        if not exists:
            db.session.add(Activity(
                title=tmpl.title, description=tmpl.description,
                activity_date=current_date,
                start_time=tmpl.start_time, end_time=tmpl.end_time,
                location=tmpl.location, category=tmpl.category,
                template_id=tmpl.id, created_by=current_user.id,
            ))
            created += 1

    db.session.commit()
    flash(f'{created} noves instancies generades per "{tmpl.title}".', 'success')
    return redirect(url_for('activities.admin_activities'))


@bp.route('/admin/activities/templates')
@admin_required
def admin_activity_templates():
    """List recurring activity templates."""
    templates = ActivityTemplate.query.order_by(ActivityTemplate.title).all()
    day_names = ['Dilluns', 'Dimarts', 'Dimecres', 'Dijous', 'Divendres', 'Dissabte', 'Diumenge']
    recurrence_labels = {'weekly': 'Setmanal', 'biweekly': 'Quinzenal', 'monthly': 'Mensual'}
    for t in templates:
        t.day_name = day_names[t.weekday] if 0 <= t.weekday <= 6 else '?'
        t.recurrence_label = recurrence_labels.get(t.recurrence, t.recurrence)
        t.instance_count = Activity.query.filter_by(template_id=t.id).count()
        t.future_count = Activity.query.filter(
            Activity.template_id == t.id, Activity.activity_date >= date.today()).count()
    return render_template('admin_activity_templates.html',
        templates=templates, categories=CATEGORIES, cat_colors=CAT_COLORS, cat_icons=CAT_ICONS)


@bp.route('/admin/activities/templates/<int:tmpl_id>/toggle', methods=['POST'])
@admin_required
def toggle_template(tmpl_id: int):
    tmpl = db.session.get(ActivityTemplate, tmpl_id)
    if tmpl:
        tmpl.active = not tmpl.active
        db.session.commit()
    return redirect(url_for('activities.admin_activity_templates'))


# ── Statistics ─────────────────────────────────────────────────────────────────

@bp.route('/admin/activities/stats')
@admin_required
def activity_stats():
    """Activity statistics and participation rates."""
    # Date range filter
    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')
    today = date.today()
    start_date = date.fromisoformat(start_str) if start_str else today - timedelta(days=30)
    end_date = date.fromisoformat(end_str) if end_str else today

    # Activities in range
    activities = Activity.query.filter(
        Activity.activity_date >= start_date, Activity.activity_date <= end_date,
    ).options(joinedload(Activity.participations)).order_by(Activity.activity_date).all()

    total_activities = len(activities)
    total_participations = 0
    total_invited = 0

    # Per-category stats
    cat_stats = {}
    for cat_key in CATEGORIES:
        cat_stats[cat_key] = {'name': CATEGORIES[cat_key], 'count': 0, 'participated': 0, 'invited': 0}

    # Per-resident stats
    resident_stats = {}
    for a in activities:
        cat = a.category or 'general'
        if cat in cat_stats:
            cat_stats[cat]['count'] += 1
        for p in a.participations:
            total_invited += 1
            if cat in cat_stats:
                cat_stats[cat]['invited'] += 1
            if p.engagement == 'participated':
                total_participations += 1
                if cat in cat_stats:
                    cat_stats[cat]['participated'] += 1

            rid = p.resident_id
            if rid not in resident_stats:
                r = p.resident
                resident_stats[rid] = {
                    'name': r.name if r else '?',
                    'room': r.room_number if r else '',
                    'invited': 0, 'participated': 0, 'refused': 0, 'absent': 0,
                }
            resident_stats[rid]['invited'] += 1
            if p.engagement in ('participated', 'passive'):
                resident_stats[rid]['participated'] += 1
            elif p.engagement == 'refused':
                resident_stats[rid]['refused'] += 1
            elif p.engagement == 'absent':
                resident_stats[rid]['absent'] += 1

    # Sort residents by participation rate (ascending = low engagement first)
    resident_list = sorted(resident_stats.values(),
        key=lambda r: r['participated'] / r['invited'] if r['invited'] > 0 else 0)

    # Low engagement alert: residents with <30% participation and >=3 invitations
    low_engagement = [r for r in resident_list
        if r['invited'] >= 3 and (r['participated'] / r['invited']) < 0.3]

    return render_template('admin_activity_stats.html',
        start_date=start_date, end_date=end_date,
        total_activities=total_activities,
        total_participations=total_participations,
        total_invited=total_invited,
        cat_stats=cat_stats, categories=CATEGORIES,
        cat_colors=CAT_COLORS, cat_icons=CAT_ICONS,
        resident_list=resident_list, low_engagement=low_engagement,
    )


@bp.route('/admin/activities/stats/export')
@admin_required
def export_activity_stats():
    """Export activity statistics to Excel."""
    import pandas as pd
    from io import BytesIO
    from flask import send_file

    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')
    today = date.today()
    start_date = date.fromisoformat(start_str) if start_str else today - timedelta(days=30)
    end_date = date.fromisoformat(end_str) if end_str else today

    activities = Activity.query.filter(
        Activity.activity_date >= start_date, Activity.activity_date <= end_date,
    ).options(
        joinedload(Activity.participations).joinedload(ActivityParticipation.resident),
    ).order_by(Activity.activity_date).all()

    rows = []
    for a in activities:
        for p in a.participations:
            r = p.resident
            rows.append({
                'Data': a.activity_date.strftime('%d/%m/%Y'),
                'Activitat': a.title,
                'Categoria': CATEGORIES.get(a.category, a.category),
                'Resident': r.name if r else '?',
                'Habitacio': r.room_number if r else '',
                'Estat': ENGAGEMENT_LABELS.get(p.engagement, p.engagement or 'Convidat'),
            })

    df = pd.DataFrame(rows)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Participacio')
    buf.seek(0)
    fname = f'activitats_{start_date}_{end_date}.xlsx'
    return send_file(buf, download_name=fname, as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── AI Activity Suggestions ────────────────────────────────────────────────────

@bp.route('/api/activities/ai-suggest', methods=['POST'])
@admin_required
def ai_suggest_activities():
    """Use Claude to suggest activities based on resident profiles and engagement history."""
    from ..blueprints.assessments import _call_claude

    residents = Resident.query.filter_by(active=True).order_by(Resident.name).all()
    today = date.today()
    thirty_days_ago = today - timedelta(days=30)

    # Build resident profiles with engagement data
    profiles = []
    for r in residents:
        participations = ActivityParticipation.query.filter(
            ActivityParticipation.resident_id == r.id,
            ActivityParticipation.activity_id.in_(
                db.session.query(Activity.id).filter(Activity.activity_date >= thirty_days_ago)
            ),
        ).all()

        total = len(participations)
        participated = sum(1 for p in participations if p.engagement == 'participated')
        refused = sum(1 for p in participations if p.engagement == 'refused')

        # Get categories they've participated in
        cat_counts = {}
        for p in participations:
            if p.engagement == 'participated':
                a = db.session.get(Activity, p.activity_id)
                if a:
                    cat_counts[a.category] = cat_counts.get(a.category, 0) + 1

        profiles.append(
            f"- {r.name} (hab.{r.room_number or '?'}, "
            f"dependencia: {r.dependency_level or 'no indicat'}, "
            f"diagnostics: {r.diagnoses or 'no indicats'}): "
            f"{participated}/{total} participacions, "
            f"{refused} refusos, "
            f"categories preferides: {', '.join(f'{CATEGORIES.get(k,k)}({v})' for k,v in sorted(cat_counts.items(), key=lambda x: -x[1])[:3]) or 'cap dada'}"
        )

    system = (
        "Ets un terapeuta ocupacional expert en residencies geriatriques. "
        "Suggereix 5 activitats per a la propera setmana basant-te en els perfils dels residents, "
        "el seu nivell de dependencia i historial de participacio. "
        "Per cada activitat inclou: titol, categoria (cognitiva/fisica/social/creativa/musical/relajacion/salida/general), "
        "dia de la setmana recomanat, duracio estimada, i una breu justificacio. "
        "Respon en JSON amb format: [{\"title\": \"..\", \"category\": \"..\", \"weekday\": \"Dilluns\", \"duration_min\": 45, \"reason\": \"...\"}]"
    )
    prompt = f"Perfils dels {len(profiles)} residents:\n" + '\n'.join(profiles)

    try:
        response = _call_claude(system, prompt)
        # Try to extract JSON from response
        import re
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if json_match:
            suggestions = json.loads(json_match.group())
        else:
            suggestions = []
        return jsonify({'suggestions': suggestions})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/admin/activities/<int:act_id>/invite', methods=['POST'])
@admin_required
def invite_residents(act_id: int):
    """Save list of invited residents for an activity."""
    data = request.get_json()
    resident_ids = data.get('resident_ids', [])

    # Remove old invitations (keep confirmed ones)
    ActivityParticipation.query.filter(
        ActivityParticipation.activity_id == act_id,
        ActivityParticipation.engagement.is_(None),
    ).delete()

    existing_ids = {p.resident_id for p in ActivityParticipation.query.filter_by(activity_id=act_id).all()}

    for rid in resident_ids:
        if rid not in existing_ids:
            db.session.add(ActivityParticipation(
                activity_id=act_id, resident_id=rid, invited=True,
            ))

    db.session.commit()
    total = ActivityParticipation.query.filter_by(activity_id=act_id).count()
    return jsonify({'ok': True, 'count': total})


@bp.route('/admin/activities/<int:act_id>/participation', methods=['POST'])
@admin_required
def save_participation(act_id: int):
    """Save participation records for an activity."""
    data = request.get_json()
    participants = data.get('participants', [])

    ActivityParticipation.query.filter_by(activity_id=act_id).delete()

    for p in participants:
        db.session.add(ActivityParticipation(
            activity_id=act_id,
            resident_id=p['resident_id'],
            invited=True,
            engagement=p.get('engagement', 'participated'),
            notes=p.get('notes', '').strip() or None,
        ))

    db.session.commit()
    return jsonify({'ok': True, 'count': len(participants)})


# ── Worker API ────────────────────────────────────────────────────────────────

@bp.route('/api/activities/today')
@jwt_required()
def api_activities_today():
    """Get today's activities with invited residents and confirmation status."""
    today = date.today()
    activities = Activity.query.filter_by(activity_date=today).options(
        joinedload(Activity.participations).joinedload(ActivityParticipation.resident),
    ).order_by(Activity.start_time).all()

    result = []
    for a in activities:
        participants = []
        for p in a.participations:
            r = p.resident
            if not r:
                continue
            participants.append({
                'resident_id': r.id,
                'name': r.name,
                'room_number': r.room_number or '',
                'photo_url': f'/api/uploads/{r.photo_path}' if r.photo_path else None,
                'engagement': p.engagement,
                'confirmed': p.engagement is not None,
            })

        result.append({
            'id': a.id,
            'title': a.title,
            'start_time': a.start_time.strftime('%H:%M') if a.start_time else None,
            'end_time': a.end_time.strftime('%H:%M') if a.end_time else None,
            'location': a.location or '',
            'category': a.category,
            'category_color': CAT_COLORS.get(a.category, '#6c757d'),
            'participants': participants,
            'total_invited': len(participants),
            'total_confirmed': sum(1 for p in participants if p['confirmed']),
        })

    return jsonify({'activities': result})


@bp.route('/api/activities/<int:act_id>/confirm', methods=['POST'])
@jwt_required()
def api_confirm_attendance(act_id: int):
    """Confirm or reject a resident's attendance at an activity."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker:
        return jsonify({'error': 'No autoritzat'}), 403

    data = request.get_json()
    resident_id = data.get('resident_id')
    status = data.get('status', 'participated')

    part = ActivityParticipation.query.filter_by(
        activity_id=act_id, resident_id=resident_id,
    ).first()

    if part:
        part.engagement = status
        part.confirmed_by = worker.id
        part.confirmed_at = datetime.now()
    else:
        db.session.add(ActivityParticipation(
            activity_id=act_id, resident_id=resident_id,
            invited=True, engagement=status,
            confirmed_by=worker.id, confirmed_at=datetime.now(),
        ))

    db.session.commit()
    return jsonify({'ok': True})
