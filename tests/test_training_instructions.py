"""Preguntas de tipo instrucción: traducciones, audio y flujo de la trabajadora."""
import json
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models import (Cleaner, TrainingPill, TrainingQuestion,
                        TrainingCompletion, TrainingTranslation)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def pill(db, admin_user):
    """Píldora con una pregunta de test y una instrucción traducida."""
    p = TrainingPill(title='Higiene de manos', pass_threshold=80,
                     video_duration_seconds=10, created_by=admin_user.id)
    db.session.add(p)
    db.session.flush()

    multiple = TrainingQuestion(
        pill_id=p.id, question_text='¿Cada cuánto hay que lavarse las manos?',
        question_type='multiple',
        option_a='Antes de cada residente', option_b='Una vez al día',
        option_c='Solo al salir', option_d='Nunca',
        correct_option='a', sort_order=0,
    )
    instruction = TrainingQuestion(
        pill_id=p.id, question_text='Lávese las manos antes y después de atender a cada residente.',
        question_type='instruccion',
        option_a='Sí', option_b='No', option_c='', option_d='',
        correct_option='a', sort_order=1,
    )
    db.session.add_all([multiple, instruction])
    db.session.flush()
    db.session.add(TrainingTranslation(
        question_id=instruction.id, lang='ar',
        text='اغسل يديك قبل وبعد رعاية كل مقيم.',
        yes_label='نعم', no_label='لا',
        audio_path=f'training_audio/q{instruction.id}_ar.mp3',
    ))
    db.session.commit()
    return p


@pytest.fixture
def worker_headers(db, cleaner_user, app):
    with app.app_context():
        token = create_access_token(identity=cleaner_user.username)
    return {'Authorization': f'Bearer {token}'}


def _ready_completion(db, pill, cleaner_user):
    """Completion con el vídeo ya visto, que es lo que exige /questions."""
    c = TrainingCompletion(
        pill_id=pill.id, cleaner_id=cleaner_user.id,
        started_at=datetime.now() - timedelta(minutes=5), video_watched=True,
    )
    db.session.add(c)
    db.session.commit()
    return c


# ── Endpoint de preguntas de la trabajadora ──────────────────────────────────

def test_las_preguntas_llevan_su_tipo(client, db, pill, cleaner_user, worker_headers):
    _ready_completion(db, pill, cleaner_user)

    res = client.get(f'/api/worker/training/{pill.id}/questions', headers=worker_headers)

    assert res.status_code == 200
    tipos = sorted(q['type'] for q in res.get_json())
    assert tipos == ['instruccion', 'multiple']


def test_la_instruccion_no_baraja_si_y_no(client, db, pill, cleaner_user, worker_headers):
    """Sí debe salir siempre primero: barajarlo confundiría a quien acaba de escuchar."""
    _ready_completion(db, pill, cleaner_user)

    for _ in range(8):  # el barajado es aleatorio: repetir para que sea concluyente
        res = client.get(f'/api/worker/training/{pill.id}/questions', headers=worker_headers)
        instr = next(q for q in res.get_json() if q['type'] == 'instruccion')
        assert instr['options'] == {'a': 'Sí', 'b': 'No'}


def test_la_instruccion_incluye_sus_traducciones(client, db, pill, cleaner_user, worker_headers):
    _ready_completion(db, pill, cleaner_user)

    res = client.get(f'/api/worker/training/{pill.id}/questions', headers=worker_headers)

    instr = next(q for q in res.get_json() if q['type'] == 'instruccion')
    arabe = next(t for t in instr['translations'] if t['lang'] == 'ar')
    assert arabe['rtl'] is True
    assert arabe['yes'] == 'نعم'
    assert arabe['audio_url'].startswith('/api/uploads/training_audio/')


def test_las_preguntas_requieren_jwt(client, db, pill):
    res = client.get(f'/api/worker/training/{pill.id}/questions')

    assert res.status_code == 401


# ── Corrección ───────────────────────────────────────────────────────────────

def test_responder_si_a_la_instruccion_cuenta_como_acierto(client, db, pill, cleaner_user, worker_headers):
    completion = _ready_completion(db, pill, cleaner_user)
    questions = client.get(f'/api/worker/training/{pill.id}/questions',
                           headers=worker_headers).get_json()
    smap = json.loads(TrainingCompletion.query.get(completion.id).shuffle_map)
    answers = {}
    for q in questions:
        idx = str(q['index'])
        if q['type'] == 'instruccion':
            answers[idx] = 'a'  # Sí
        else:
            # Localizar la clave barajada que corresponde a la opción correcta.
            answers[idx] = next(k for k, v in smap[idx]['option_map'].items() if v == 'a')

    res = client.post(f'/api/worker/training/{pill.id}/submit',
                      json={'worker_id': cleaner_user.id, 'answers': answers},
                      headers=worker_headers)

    assert res.status_code == 200
    assert res.get_json()['score'] == 100


def test_responder_no_a_la_instruccion_cuenta_como_fallo(client, db, pill, cleaner_user, worker_headers):
    completion = _ready_completion(db, pill, cleaner_user)
    questions = client.get(f'/api/worker/training/{pill.id}/questions',
                           headers=worker_headers).get_json()
    smap = json.loads(TrainingCompletion.query.get(completion.id).shuffle_map)
    answers = {}
    for q in questions:
        idx = str(q['index'])
        if q['type'] == 'instruccion':
            answers[idx] = 'b'  # No
        else:
            answers[idx] = next(k for k, v in smap[idx]['option_map'].items() if v == 'a')

    res = client.post(f'/api/worker/training/{pill.id}/submit',
                      json={'worker_id': cleaner_user.id, 'answers': answers},
                      headers=worker_headers)

    data = res.get_json()
    assert data['score'] == 50
    assert data['passed'] is False


# ── Edición de la píldora: no debe destruir las traducciones ─────────────────

def test_editar_la_pildora_conserva_las_traducciones(auth_client, db, pill):
    instr = next(q for q in pill.questions if q.question_type == 'instruccion')
    multiple = next(q for q in pill.questions if q.question_type == 'multiple')
    instr_id = instr.id

    auth_client.post(f'/admin/training/{pill.id}/edit', data={
        'title': 'Higiene de manos (revisado)',
        'pass_threshold': '80',
        'q_0_id': multiple.id, 'q_0_type': 'multiple',
        'q_0_text': multiple.question_text,
        'q_0_a': multiple.option_a, 'q_0_b': multiple.option_b,
        'q_0_c': multiple.option_c, 'q_0_d': multiple.option_d,
        'q_0_correct': 'a',
        'q_1_id': instr_id, 'q_1_type': 'instruccion',
        'q_1_text': instr.question_text,
    }, follow_redirects=True)

    assert TrainingTranslation.query.filter_by(question_id=instr_id).count() == 1
    assert db.session.get(TrainingPill, pill.id).title == 'Higiene de manos (revisado)'


def test_cambiar_el_texto_de_la_instruccion_descarta_su_traduccion(auth_client, db, pill):
    """Una traducción que ya no se corresponde con el original engaña más que ayuda."""
    instr = next(q for q in pill.questions if q.question_type == 'instruccion')
    instr_id = instr.id

    auth_client.post(f'/admin/training/{pill.id}/edit', data={
        'title': pill.title,
        'pass_threshold': '80',
        'q_0_id': instr_id, 'q_0_type': 'instruccion',
        'q_0_text': 'Use guantes al manipular residuos.',
    }, follow_redirects=True)

    assert TrainingTranslation.query.filter_by(question_id=instr_id).count() == 0


def test_crear_una_instruccion_rellena_si_y_no(auth_client, db, admin_user):
    auth_client.post('/admin/training/create', data={
        'title': 'Uso de guantes',
        'pass_threshold': '80',
        'q_0_type': 'instruccion',
        'q_0_text': 'Póngase guantes antes de manipular residuos.',
    }, follow_redirects=True)

    q = TrainingQuestion.query.filter_by(question_type='instruccion').first()
    assert q is not None
    assert (q.option_a, q.option_b, q.correct_option) == ('Sí', 'No', 'a')


# ── Autorización de las rutas nuevas ─────────────────────────────────────────

def test_pantalla_de_idiomas_requiere_admin(client, db, pill):
    res = client.get(f'/admin/training/{pill.id}/languages')

    assert res.status_code in (302, 401, 403)


def test_traducir_requiere_admin(client, db, pill):
    instr = next(q for q in pill.questions if q.question_type == 'instruccion')

    res = client.post(f'/api/training/question/{instr.id}/translate', json={'langs': ['ar']})

    assert res.status_code in (302, 401, 403)


def test_generar_audio_requiere_admin(client, db, pill):
    instr = next(q for q in pill.questions if q.question_type == 'instruccion')

    res = client.post(f'/api/training/question/{instr.id}/audio', json={'langs': ['ar']})

    assert res.status_code in (302, 401, 403)


def test_guardar_traduccion_requiere_admin(client, db, pill):
    instr = next(q for q in pill.questions if q.question_type == 'instruccion')

    res = client.put(f'/api/training/question/{instr.id}/translation/ar', json={'text': 'x'})

    assert res.status_code in (302, 401, 403)


def test_traducir_una_pregunta_de_test_devuelve_404(auth_client, db, pill):
    """Solo las instrucciones se traducen; un test A/B/C/D no."""
    multiple = next(q for q in pill.questions if q.question_type == 'multiple')

    res = auth_client.post(f'/api/training/question/{multiple.id}/translate',
                           json={'langs': ['ar']})

    assert res.status_code == 404


def test_generar_audio_sin_api_key_avisa_en_castellano(auth_client, db, pill, app):
    instr = next(q for q in pill.questions if q.question_type == 'instruccion')
    original = app.config.get('OPENAI_API_KEY')
    app.config['OPENAI_API_KEY'] = None
    try:
        res = auth_client.post(f'/api/training/question/{instr.id}/audio',
                               json={'langs': ['ar']})
    finally:
        app.config['OPENAI_API_KEY'] = original

    assert res.status_code == 503
    assert 'OPENAI_API_KEY' in res.get_json()['error']


# ── Guardado manual de una traducción ────────────────────────────────────────

def test_guardar_traduccion_a_mano_invalida_el_audio(auth_client, db, pill):
    instr = next(q for q in pill.questions if q.question_type == 'instruccion')

    res = auth_client.put(f'/api/training/question/{instr.id}/translation/ar',
                          json={'text': 'نص جديد', 'yes': 'نعم', 'no': 'لا'})

    assert res.status_code == 200
    tr = TrainingTranslation.query.filter_by(question_id=instr.id, lang='ar').first()
    assert tr.text == 'نص جديد'
    assert tr.audio_path is None


def test_guardar_traduccion_vacia_se_rechaza(auth_client, db, pill):
    instr = next(q for q in pill.questions if q.question_type == 'instruccion')

    res = auth_client.put(f'/api/training/question/{instr.id}/translation/ar',
                          json={'text': '   '})

    assert res.status_code == 400
