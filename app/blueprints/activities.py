"""Activities calendar — scheduling and participation tracking."""
from __future__ import annotations
from datetime import datetime, timedelta, date

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_login import current_user
from sqlalchemy.orm import joinedload

from .. import db
from ..models import Activity, ActivityParticipation, Resident
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

    return render_template('admin_activities.html',
        days=days, by_day=by_day, view_date=view_date,
        monday=monday, categories=CATEGORIES, cat_icons=CAT_ICONS, cat_colors=CAT_COLORS,
        engagement_labels=ENGAGEMENT_LABELS, residents=residents,
        prev_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        is_current_week=date.today() in days,
    )


@bp.route('/admin/activities/save', methods=['POST'])
@admin_required
def save_activity():
    act_id = request.form.get('activity_id', type=int)
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
    flash(f'Activitat {"actualitzada" if act_id else "creada"}: {a.title}', 'success')
    return redirect(url_for('activities.admin_activities', date=a.activity_date.isoformat()))


@bp.route('/admin/activities/<int:act_id>/delete', methods=['POST'])
@admin_required
def delete_activity(act_id: int):
    a = db.session.get(Activity, act_id)
    if a:
        act_date = a.activity_date.isoformat()
        db.session.delete(a)
        db.session.commit()
        flash('Activitat eliminada.', 'success')
        return redirect(url_for('activities.admin_activities', date=act_date))
    return redirect(url_for('activities.admin_activities'))


@bp.route('/admin/activities/<int:act_id>/participation', methods=['POST'])
@admin_required
def save_participation(act_id: int):
    """Save participation records for an activity."""
    data = request.get_json()
    participants = data.get('participants', [])

    # Clear existing
    ActivityParticipation.query.filter_by(activity_id=act_id).delete()

    for p in participants:
        db.session.add(ActivityParticipation(
            activity_id=act_id,
            resident_id=p['resident_id'],
            engagement=p.get('engagement', 'participated'),
            notes=p.get('notes', '').strip() or None,
        ))

    db.session.commit()
    return jsonify({'ok': True, 'count': len(participants)})
