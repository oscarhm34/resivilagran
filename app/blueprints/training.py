"""Training pills blueprint — admin CRUD + worker API."""
from __future__ import annotations
import json
import random
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, abort
from flask_login import current_user
from flask_jwt_extended import jwt_required
from sqlalchemy.orm import joinedload

from .. import db, app, limiter
from ..models import (Cleaner, TrainingPill, TrainingQuestion, TrainingCompletion,
                      TrainingAssignment, TrainingTranslation)
from ..utils import (admin_required, _verify_worker_id, _current_worker_id,
                     log_audit, _safe_commit, TRAINING_LANGUAGES)

bp = Blueprint('training', __name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _trainable_workers():
    """Personas que pueden recibir formacion.

    Incluye a los administradores: la direccion y la coordinacion tambien hacen
    las formaciones obligatorias, y ademas necesitan poder asignarse una pildora
    para probarla antes de darla al resto del equipo.
    """
    return Cleaner.query.filter_by(active=True).order_by(Cleaner.name)



def _question_fields_from_form(idx: int) -> dict:
    """Lee del formulario los campos de la pregunta `idx`.

    Las preguntas de tipo `instruccion` reutilizan option_a/option_b como
    Si/No y correct_option='a'. Asi el corrector y el barajado existentes
    siguen funcionando sin necesidad de columnas nuevas.
    """
    qtype = (request.form.get(f'q_{idx}_type') or 'multiple').strip()
    if qtype not in ('multiple', 'instruccion'):
        qtype = 'multiple'
    if qtype == 'instruccion':
        return {
            'question_text': request.form[f'q_{idx}_text'].strip(),
            'question_type': 'instruccion',
            'option_a': 'Sí', 'option_b': 'No', 'option_c': '', 'option_d': '',
            'correct_option': 'a',
            'sort_order': idx,
        }
    return {
        'question_text': request.form[f'q_{idx}_text'].strip(),
        'question_type': 'multiple',
        'option_a': request.form.get(f'q_{idx}_a', '').strip(),
        'option_b': request.form.get(f'q_{idx}_b', '').strip(),
        'option_c': request.form.get(f'q_{idx}_c', '').strip(),
        'option_d': request.form.get(f'q_{idx}_d', '').strip(),
        'correct_option': request.form.get(f'q_{idx}_correct', 'a').strip(),
        'sort_order': idx,
    }


def _audio_dir() -> str:
    import os
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'training_audio')
    os.makedirs(folder, exist_ok=True)
    return folder


def _discard_audio(tr: TrainingTranslation) -> None:
    """Borra el MP3 de una traduccion cuyo texto ha cambiado.

    Un audio que ya no se corresponde con el texto es peor que no tener audio.
    """
    import os
    if not tr.audio_path:
        return
    path = os.path.join(app.config['UPLOAD_FOLDER'], tr.audio_path)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            app.logger.error('Error al borrar el audio de la instruccion: %s', e)
    tr.audio_path = None


# ── Admin ────────────────────────────────────────────────────────────────────

@bp.route('/admin/training')
@admin_required
def admin_training():
    pills = TrainingPill.query.order_by(TrainingPill.created_at.desc()).all()
    total_workers = _trainable_workers().count()
    pills_json = {p.id: {
        'title': p.title, 'description': p.description or '',
        'video_url': p.video_url or '',
        'video_duration_seconds': p.video_duration_seconds or '',
        'pass_threshold': p.pass_threshold,
        'assign_mode': p.assign_mode or 'all',
        'mandatory': p.mandatory or False,
        'assigned_worker_ids': [a.cleaner_id for a in p.assignments],
        'questions': [{
            'id': q.id,
            'question_text': q.question_text,
            'question_type': q.question_type or 'multiple',
            'option_a': q.option_a, 'option_b': q.option_b,
            'option_c': q.option_c, 'option_d': q.option_d,
            'correct_option': q.correct_option,
        } for q in p.questions],
        'has_instructions': any((q.question_type or 'multiple') == 'instruccion'
                                for q in p.questions),
    } for p in pills}
    workers = _trainable_workers().all()
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
        db.session.add(TrainingQuestion(pill_id=pill.id, **_question_fields_from_form(idx)))
        idx += 1
    # Assignments for selected mode
    if assign_mode == 'selected':
        worker_ids = request.form.getlist('assigned_workers')
        for wid in worker_ids:
            db.session.add(TrainingAssignment(
                pill_id=pill.id, cleaner_id=int(wid), assigned_by=current_user.id))
    log_audit('create', 'training_pill', pill.id, {'titulo': pill.title})
    ok, error = _safe_commit('Error al crear la pildora formativa')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('training.admin_training'))
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
    # Reconciliar las preguntas en vez de borrarlas y recrearlas: borrarlas se
    # llevaba por delante las traducciones y los audios de las instrucciones
    # (y dejaba invalidos los shuffle_map de los tests en curso).
    existing = {q.id: q for q in pill.questions}
    seen: set[int] = set()
    idx = 0
    while request.form.get(f'q_{idx}_text'):
        fields = _question_fields_from_form(idx)
        qid = request.form.get(f'q_{idx}_id', type=int)
        q = existing.get(qid) if qid else None
        if q is not None:
            was_instruction = (q.question_type or 'multiple') == 'instruccion'
            text_changed = q.question_text != fields['question_text']
            for key, value in fields.items():
                setattr(q, key, value)
            # Si cambia el texto de una instruccion, sus traducciones y audios
            # dejan de corresponderse con el original: se descartan.
            if was_instruction and text_changed:
                for tr in list(q.translations):
                    _discard_audio(tr)
                    db.session.delete(tr)
            seen.add(q.id)
        else:
            db.session.add(TrainingQuestion(pill_id=pill.id, **fields))
        idx += 1
    for qid, q in existing.items():
        if qid not in seen:
            for tr in list(q.translations):
                _discard_audio(tr)
            db.session.delete(q)
    log_audit('update', 'training_pill', pill.id, {'titulo': pill.title})
    ok, error = _safe_commit('Error al actualizar la pildora formativa')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('training.admin_training'))
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
    log_audit('delete', 'training_pill', pill_id, {'titulo': pill.title})
    db.session.delete(pill)
    ok, error = _safe_commit('Error al eliminar la pildora formativa')
    if not ok:
        flash(error, 'danger')
    else:
        flash('Píldora formativa eliminada.', 'success')
    return redirect(url_for('training.admin_training'))


@bp.route('/admin/training/<int:pill_id>/toggle', methods=['POST'])
@admin_required
def toggle_training(pill_id: int):
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    pill.active = not pill.active
    log_audit('update', 'training_pill', pill_id, {'activa': pill.active})
    ok, error = _safe_commit('Error al cambiar el estado de la pildora')
    if not ok:
        flash(error, 'danger')
        return redirect(url_for('training.admin_training'))
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
        workers = _trainable_workers().all()
    completed_ids = {c.cleaner_id for c in completions if c.passed}
    pending = [w for w in workers if w.id not in completed_ids]
    return render_template('admin_training_results.html',
                           pill=pill, completions=completions, pending=pending)


# ── AI Question Generation ───────────────────────────────────────────────────

@bp.route('/api/training/ai-generate-questions', methods=['POST'])
@limiter.limit("5/minute")
@admin_required
def ai_generate_questions():
    """Use AI to generate quiz questions from pill title and description."""
    from ..blueprints.assessments import _call_claude

    data = request.get_json() or {}
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()

    if not title:
        return jsonify({'error': 'El título es obligatorio'}), 400

    system = (
        "Eres un formador de residencias geriatricas. "
        "Genera preguntas tipo test para evaluar la comprension de una formacion. "
        "Cada pregunta tiene 4 opciones (A, B, C, D) y una respuesta correcta. "
        "Las preguntas deben ser claras, practicas y relevantes para el trabajo en residencias. "
        "Responde SOLO en JSON con formato: "
        '[{"question_text": "...", "option_a": "...", "option_b": "...", "option_c": "...", "option_d": "...", "correct_option": "a"}]'
    )

    content_desc = f"Título: {title}"
    if description:
        content_desc += f"\nDescripción/contenido: {description}"

    num_questions = data.get('num_questions', 5)
    prompt = f"Genera {num_questions} preguntas tipo test sobre esta formacion:\n\n{content_desc}"

    try:
        import re
        response = _call_claude(system, prompt)
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if json_match:
            questions = json.loads(json_match.group())
        else:
            questions = []
        return jsonify({'questions': questions})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Idiomas y audio de las instrucciones ─────────────────────────────────────

@bp.route('/admin/training/<int:pill_id>/languages')
@admin_required
def admin_training_languages(pill_id: int):
    """Pantalla de gestion de traducciones y audios de una pildora."""
    pill = db.session.get(TrainingPill, pill_id)
    if not pill:
        abort(404)
    instructions = [q for q in pill.questions
                    if (q.question_type or 'multiple') == 'instruccion']
    return render_template('admin_training_languages.html', pill=pill,
                           instructions=instructions, languages=TRAINING_LANGUAGES)


def _get_instruction(q_id: int):
    """Devuelve la pregunta si existe y es de tipo instruccion."""
    q = db.session.get(TrainingQuestion, q_id)
    if not q or (q.question_type or 'multiple') != 'instruccion':
        return None
    return q


def _upsert_translation(q, lang: str, text: str, yes_label: str, no_label: str):
    tr = q.translation(lang)
    if tr is None:
        tr = TrainingTranslation(question_id=q.id, lang=lang, text=text,
                                 yes_label=yes_label, no_label=no_label)
        db.session.add(tr)
        return tr
    if tr.text != text:
        _discard_audio(tr)
    tr.text = text
    tr.yes_label = yes_label
    tr.no_label = no_label
    tr.generated_at = datetime.now()
    return tr


@bp.route('/api/training/question/<int:q_id>/translate', methods=['POST'])
@admin_required
@limiter.limit("5/minute", methods=["POST"])
def translate_instruction(q_id: int):
    """Traduce el texto de una instruccion a los idiomas pedidos con IA."""
    from ..blueprints.assessments import _call_claude

    q = _get_instruction(q_id)
    if not q:
        return jsonify({'error': 'La pregunta no existe o no es una instrucción.'}), 404

    data = request.get_json() or {}
    langs = [l for l in (data.get('langs') or []) if l in TRAINING_LANGUAGES and l != 'es']
    if not langs:
        return jsonify({'error': 'Selecciona al menos un idioma.'}), 400

    # El español siempre existe: es el texto original que escribió el administrador.
    _upsert_translation(q, 'es', q.question_text, 'Sí', 'No')

    names = ', '.join(f"{code} ({TRAINING_LANGUAGES[code]['name']})" for code in langs)
    system = (
        "Eres traductor profesional para una residencia de personas mayores. "
        "Traduces instrucciones de trabajo dirigidas a personal de limpieza y atencion "
        "que puede tener poca formacion. Usa lenguaje llano, frases cortas y trato de usted. "
        "No añadas explicaciones ni cambies el significado. "
        "Traduce tambien las etiquetas de respuesta afirmativa y negativa. "
        "Responde SOLO con un objeto JSON, sin texto alrededor, con esta forma: "
        '{"ar": {"text": "...", "yes": "...", "no": "..."}}'
    )
    prompt = (f"Traduce este texto a los idiomas {names}.\n\n"
              f"Texto original en español:\n{q.question_text}")

    try:
        response = _call_claude(system, prompt)
        import re as _re
        match = _re.search(r'\{.*\}', response, _re.DOTALL)
        if not match:
            return jsonify({'error': 'La IA no ha devuelto una traducción válida.'}), 502
        payload = json.loads(match.group())
    except (ValueError, json.JSONDecodeError) as e:
        app.logger.error('Error al traducir la instruccion: %s', e)
        return jsonify({'error': 'No se ha podido generar la traducción.'}), 502

    done = []
    for lang in langs:
        entry = payload.get(lang) or {}
        text = (entry.get('text') or '').strip()
        if not text:
            continue
        _upsert_translation(q, lang, text,
                            (entry.get('yes') or 'Sí').strip(),
                            (entry.get('no') or 'No').strip())
        done.append(lang)

    if not done:
        return jsonify({'error': 'La IA no ha devuelto ninguna traducción utilizable.'}), 502

    log_audit('translate', 'training_question', q.id, {'langs': done})
    ok, error = _safe_commit('Error al guardar las traducciones')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'langs': done,
                    'translations': _translations_payload(q)})


@bp.route('/api/training/question/<int:q_id>/translation/<lang>', methods=['PUT'])
@admin_required
def update_translation(q_id: int, lang: str):
    """Guarda una correccion manual del texto traducido."""
    q = _get_instruction(q_id)
    if not q:
        return jsonify({'error': 'La pregunta no existe o no es una instrucción.'}), 404
    if lang not in TRAINING_LANGUAGES:
        return jsonify({'error': 'Idioma no soportado.'}), 400

    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'El texto no puede estar vacío.'}), 400

    _upsert_translation(q, lang, text,
                        (data.get('yes') or 'Sí').strip(),
                        (data.get('no') or 'No').strip())
    log_audit('update_translation', 'training_question', q.id, {'lang': lang})
    ok, error = _safe_commit('Error al guardar la traducción')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'translations': _translations_payload(q)})


@bp.route('/api/training/question/<int:q_id>/audio', methods=['POST'])
@admin_required
@limiter.limit("5/minute", methods=["POST"])
def generate_instruction_audio(q_id: int):
    """Genera el MP3 de cada idioma con el TTS de OpenAI."""
    import os
    import requests

    q = _get_instruction(q_id)
    if not q:
        return jsonify({'error': 'La pregunta no existe o no es una instrucción.'}), 404

    api_key = app.config.get('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'error': 'Audio no configurado (falta OPENAI_API_KEY)'}), 503

    data = request.get_json() or {}
    langs = [l for l in (data.get('langs') or []) if l in TRAINING_LANGUAGES]
    if not langs:
        return jsonify({'error': 'Selecciona al menos un idioma.'}), 400

    folder = _audio_dir()
    done, failed = [], []
    for lang in langs:
        tr = q.translation(lang)
        if not tr or not tr.text.strip():
            failed.append(lang)
            continue
        try:
            res = requests.post(
                'https://api.openai.com/v1/audio/speech',
                headers={'Authorization': f'Bearer {api_key}'},
                json={
                    'model': 'gpt-4o-mini-tts',
                    'voice': TRAINING_LANGUAGES[lang]['voice'],
                    'input': tr.text,
                    'response_format': 'mp3',
                },
                timeout=60,
            )
            if res.status_code != 200:
                app.logger.error('TTS devolvio %s para el idioma %s', res.status_code, lang)
                failed.append(lang)
                continue
            filename = f'q{q.id}_{lang}.mp3'
            with open(os.path.join(folder, filename), 'wb') as fh:
                fh.write(res.content)
            tr.audio_path = f'training_audio/{filename}'
            tr.generated_at = datetime.now()
            done.append(lang)
        except requests.RequestException as e:
            app.logger.error('Error de red al generar el audio (%s): %s', lang, e)
            failed.append(lang)

    if not done:
        return jsonify({'error': 'No se ha podido generar ningún audio.',
                        'failed': failed}), 502

    log_audit('generate_audio', 'training_question', q.id, {'langs': done})
    ok, error = _safe_commit('Error al guardar los audios')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'langs': done, 'failed': failed,
                    'translations': _translations_payload(q)})


def _translations_payload(q) -> list[dict]:
    """Estado de cada idioma de una instruccion, para refrescar la pantalla."""
    return [{
        'lang': t.lang,
        'text': t.text,
        'yes': t.yes_label,
        'no': t.no_label,
        'audio_url': (url_for('nfc.serve_upload', filename=t.audio_path)
                      if t.audio_path else None),
    } for t in q.translations]


# ── Worker API ───────────────────────────────────────────────────────────────

@bp.route('/api/worker/pending-training')
@jwt_required()
def pending_training():
    worker_id = _current_worker_id()
    if not worker_id:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
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
    ok, error = _safe_commit('Error al iniciar la formacion')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'completion_id': completion.id})


@bp.route('/api/worker/training/<int:pill_id>/video-complete', methods=['POST'])
@jwt_required()
def training_video_complete(pill_id: int):
    worker_id = _current_worker_id()
    if not worker_id:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
    completion = TrainingCompletion.query.filter_by(
        pill_id=pill_id, cleaner_id=worker_id,
    ).order_by(TrainingCompletion.started_at.desc()).first()
    if not completion:
        return jsonify({'error': 'No hay sesión activa'}), 400
    pill = db.session.get(TrainingPill, pill_id)
    # Una pildora de solo instrucciones no lleva video: no hay nada que ver, asi
    # que no se hace esperar a nadie delante de un recuadro vacio.
    if pill.video_url:
        min_secs = (pill.video_duration_seconds or 60) * 0.5
        elapsed = (datetime.now() - completion.started_at).total_seconds()
        if elapsed < min_secs:
            return jsonify({'error': 'Debes ver el vídeo completo', 'wait': int(min_secs - elapsed)}), 400
    completion.video_watched = True
    ok, error = _safe_commit('Error al registrar el visionado del video')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True})


@bp.route('/api/worker/training/<int:pill_id>/questions')
@jwt_required()
def training_questions(pill_id: int):
    worker_id = _current_worker_id()
    if not worker_id:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
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
        qtype = q.question_type or 'multiple'
        if qtype == 'instruccion':
            # Si/No siempre en el mismo orden: barajarlas confundiria a quien
            # acaba de escuchar la instruccion. option_map es la identidad para
            # que el corrector siga funcionando igual.
            option_map = {'a': 'a', 'b': 'b'}
            item = {
                'index': i,
                'type': 'instruccion',
                'question': q.question_text,
                'options': {'a': q.option_a, 'b': q.option_b},
                'translations': [{
                    'lang': t.lang,
                    'name': TRAINING_LANGUAGES.get(t.lang, {}).get('native', t.lang),
                    'flag': TRAINING_LANGUAGES.get(t.lang, {}).get('flag', ''),
                    'rtl': TRAINING_LANGUAGES.get(t.lang, {}).get('rtl', False),
                    'text': t.text,
                    'yes': t.yes_label,
                    'no': t.no_label,
                    'audio_url': f'/api/uploads/{t.audio_path}' if t.audio_path else None,
                } for t in sorted(q.translations, key=lambda t: t.lang != 'es')],
            }
        else:
            options = [('a', q.option_a), ('b', q.option_b), ('c', q.option_c), ('d', q.option_d)]
            random.shuffle(options)
            option_map = {}
            shuffled_options = {}
            for new_key, (orig_key, text) in zip(['a', 'b', 'c', 'd'], options):
                option_map[new_key] = orig_key
                shuffled_options[new_key] = text
            item = {
                'index': i,
                'type': 'multiple',
                'question': q.question_text,
                'options': shuffled_options,
            }
        shuffle_map[str(i)] = {'question_id': q.id, 'option_map': option_map}
        result.append(item)
    completion.shuffle_map = json.dumps(shuffle_map)
    ok, error = _safe_commit('Error al preparar el cuestionario')
    if not ok:
        return jsonify({'error': error}), 500
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
    ok, error = _safe_commit('Error al guardar el resultado del cuestionario')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({
        'score': score,
        'passed': completion.passed,
        'correct': correct,
        'total': total,
        'threshold': pill.pass_threshold,
    })
