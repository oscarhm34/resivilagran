"""Training pills blueprint — admin CRUD + worker API."""
from __future__ import annotations
import json
import random
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, abort
from flask_login import current_user
from flask_jwt_extended import jwt_required
from sqlalchemy.orm import joinedload

from .. import db
from ..models import (Cleaner, TrainingPill, TrainingQuestion, TrainingCompletion, TrainingAssignment)
from ..utils import admin_required, _verify_worker_id

bp = Blueprint('training', __name__)


# ── Admin ────────────────────────────────────────────────────────────────────

@bp.route('/admin/training')
@admin_required
def admin_training():
    pills = TrainingPill.query.order_by(TrainingPill.created_at.desc()).all()
    total_workers = Cleaner.query.filter_by(active=True, is_admin=False).count()
    pills_json = {p.id: {
        'title': p.title, 'description': p.description or '',
        'video_url': p.video_url or '',
        'video_duration_seconds': p.video_duration_seconds or '',
        'pass_threshold': p.pass_threshold,
        'assign_mode': p.assign_mode or 'all',
        'mandatory': p.mandatory or False,
        'assigned_worker_ids': [a.cleaner_id for a in p.assignments],
        'questions': [{
            'question_text': q.question_text,
            'option_a': q.option_a, 'option_b': q.option_b,
            'option_c': q.option_c, 'option_d': q.option_d,
            'correct_option': q.correct_option,
        } for q in p.questions],
    } for p in pills}
    workers = Cleaner.query.filter_by(active=True, is_admin=False).order_by(Cleaner.name).all()
    return render_template('admin_training.html', pills=pills,
                           total_workers=total_workers, pills_json=pills_json, workers=workers)


@bp.route('/admin/training/create', methods=['POST'])
@admin_required
def create_training():
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    video_url = request.form.get('video_url', '').strip()
    duration = request.form.get('video_duration_seconds', type=int) or None
    threshold = request.form.get('pass_threshold', 80, type=int)
    if not title:
        flash('El título es obligatorio.', 'error')
        return redirect(url_for('training.admin_training'))
    assign_mode = request.form.get('assign_mode', 'all')
    mandatory = request.form.get('mandatory') == 'on'
    pill = TrainingPill(
        title=title, description=description or None,
        video_url=video_url or None, video_duration_seconds=duration,
        pass_threshold=threshold, created_by=current_user.id,
        assign_mode=assign_mode, mandatory=mandatory,
    )
    db.session.add(pill)
    db.session.flush()
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
    # Assignments for selected mode
    if assign_mode == 'selected':
        worker_ids = request.form.getlist('assigned_workers')
        for wid in worker_ids:
            db.session.add(TrainingAssignment(
                pill_id=pill.id, cleaner_id=int(wid), assigned_by=current_user.id))
    db.session.commit()
    flash('Píldora formativa creada.', 'success')
    return redirect(url_for('training.admin_training'))


@bp.route('/admin/training/<int:pill_id>/edit', methods=['POST'])
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
    pill.assign_mode = request.form.get('assign_mode', 'all')
    pill.mandatory = request.form.get('mandatory') == 'on'
    # Update assignments
    TrainingAssignment.query.filter_by(pill_id=pill.id).delete()
    if pill.assign_mode == 'selected':
        worker_ids = request.form.getlist('assigned_workers')
        for wid in worker_ids:
            db.session.add(TrainingAssignment(
                pill_id=pill.id, cleaner_id=int(wid), assigned_by=current_user.id))
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
    return redirect(url_for('training.admin_training'))


@bp.route('/admin/training/<int:pill_id>/delete', methods=['POST'])
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
    return redirect(url_for('training.admin_training'))


@bp.route('/admin/training/<int:pill_id>/toggle', methods=['POST'])
@admin_required
def toggle_training(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    pill.active = not pill.active
    db.session.commit()
    flash(f'Píldora {"activada" if pill.active else "desactivada"}.', 'success')
    return redirect(url_for('training.admin_training'))


@bp.route('/admin/training/<int:pill_id>/results')
@admin_required
def training_results(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    completions = TrainingCompletion.query.filter_by(pill_id=pill_id)\
        .options(joinedload(TrainingCompletion.cleaner))\
        .order_by(TrainingCompletion.completed_at.desc()).all()
    if pill.assign_mode == 'selected':
        assigned_ids = {a.cleaner_id for a in pill.assignments}
        workers = Cleaner.query.filter(
            Cleaner.id.in_(assigned_ids), Cleaner.active == True
        ).order_by(Cleaner.name).all()
    else:
        workers = Cleaner.query.filter_by(active=True, is_admin=False).order_by(Cleaner.name).all()
    completed_ids = {c.cleaner_id for c in completions if c.passed}
    pending = [w for w in workers if w.id not in completed_ids]
    return render_template('admin_training_results.html',
                           pill=pill, completions=completions, pending=pending)


# ── Worker API ───────────────────────────────────────────────────────────────

@bp.route('/api/worker/pending-training')
@jwt_required()
def pending_training():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    passed = db.session.query(TrainingCompletion.pill_id)\
        .filter_by(cleaner_id=worker_id, passed=True).subquery()
    all_pending = TrainingPill.query.filter_by(active=True)\
        .filter(~TrainingPill.id.in_(passed))\
        .order_by(TrainingPill.created_at).all()

    # Filter by assignment mode
    assigned_pill_ids = {a.pill_id for a in
        TrainingAssignment.query.filter_by(cleaner_id=worker_id).all()}
    pills = []
    for p in all_pending:
        if p.assign_mode == 'all':
            pills.append(p)
        elif p.assign_mode == 'selected' and p.id in assigned_pill_ids:
            pills.append(p)

    return jsonify([{
        'id': p.id, 'title': p.title,
        'description': p.description or '',
        'question_count': len(p.questions),
        'mandatory': p.mandatory or False,
    } for p in pills])


@bp.route('/api/worker/training/<int:pill_id>')
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


@bp.route('/api/worker/training/<int:pill_id>/start', methods=['POST'])
@jwt_required()
def start_training(pill_id: int):
    from .nfc import _save_base64_photo
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
    if photo:
        try:
            _save_base64_photo(photo, 'selfies', int(worker_id))
        except ValueError:
            pass
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


@bp.route('/api/worker/training/<int:pill_id>/video-complete', methods=['POST'])
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
    pill = db.session.get(TrainingPill, pill_id)
    min_secs = (pill.video_duration_seconds or 60) * 0.5
    elapsed = (datetime.now() - completion.started_at).total_seconds()
    if elapsed < min_secs:
        return jsonify({'error': 'Debes ver el vídeo completo', 'wait': int(min_secs - elapsed)}), 400
    completion.video_watched = True
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/worker/training/<int:pill_id>/questions')
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
    questions = list(pill.questions)
    random.shuffle(questions)
    shuffle_map = {}
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


@bp.route('/api/worker/training/<int:pill_id>/submit', methods=['POST'])
@jwt_required()
def submit_training(pill_id: int):
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    answers = data.get('answers', {})
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
