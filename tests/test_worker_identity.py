"""
test_worker_identity.py — Cruce de identidad en la API JWT de la webapp.

Las rutas `@jwt_required()` aceptaban un `worker_id` / `cleaner_id` enviado por
el cliente y operaban con él sin comprobarlo, así que cualquier trabajadora
autenticada podía leer la jornada, los documentos o la formación de otra, y
abrir o cerrar limpiezas a su nombre.

Ahora la identidad sale siempre del token. Estos tests fijan ese contrato: el
parámetro del cliente se sigue aceptando (compatibilidad con `worker.html`)
pero se ignora.
"""

from datetime import datetime, timedelta

import pytest

from app.models import (
    Cleaner, CleaningRecord, DocumentSignature, LegalDocument, ResidentGroup,
    TrainingCompletion, TrainingPill,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def otra_trabajadora(db):
    """Segunda trabajadora: la víctima del cruce de identidad."""
    u = Cleaner(username='limpiadora2', name='Otra Trabajadora', is_admin=False)
    u.set_password('limpia456')
    db.session.add(u)
    db.session.commit()
    return u


def _token(client, username, password):
    resp = client.post('/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['access_token']


@pytest.fixture
def atacante(client, cleaner_user):
    """Cliente autenticado como `cleaner_user`, que intentará suplantar."""
    client.environ_base['HTTP_AUTHORIZATION'] = (
        f"Bearer {_token(client, 'limpiadora1', 'limpia123')}"
    )
    return client


@pytest.fixture
def limpieza_ajena(db, otra_trabajadora, room):
    """Limpieza en curso que pertenece a la otra trabajadora."""
    r = CleaningRecord(cleaner_id=otra_trabajadora.id, room_id=room.id,
                       start_time=datetime.now(), end_time=None)
    db.session.add(r)
    db.session.commit()
    return r


# ── Limpiezas ─────────────────────────────────────────────────────────────────

class TestLimpiezas:

    def test_start_cleaning_ignora_el_cleaner_id_del_cliente(
        self, atacante, cleaner_user, otra_trabajadora, room, db
    ):
        resp = atacante.post('/start_cleaning', json={
            'cleaner_id': otra_trabajadora.id, 'room_id': room.number,
        })

        assert resp.status_code == 200
        registro = db.session.get(CleaningRecord, resp.get_json()['record_id'])
        assert registro.cleaner_id == cleaner_user.id
        assert CleaningRecord.query.filter_by(
            cleaner_id=otra_trabajadora.id).count() == 0

    def test_end_cleaning_no_puede_cerrar_la_limpieza_de_otra(
        self, atacante, limpieza_ajena, db
    ):
        resp = atacante.post('/end_cleaning', json={'record_id': limpieza_ajena.id})

        assert resp.status_code == 403
        assert db.session.get(CleaningRecord, limpieza_ajena.id).end_time is None

    def test_check_cleaning_ignora_el_cleaner_id_del_cliente(
        self, atacante, limpieza_ajena, otra_trabajadora
    ):
        resp = atacante.get(f'/check_cleaning?cleaner_id={otra_trabajadora.id}')

        assert resp.status_code == 200
        assert 'room_id' not in resp.get_json()      # el atacante no tiene ninguna

    def test_cleaning_summary_de_otra_trabajadora_devuelve_403(
        self, atacante, otra_trabajadora
    ):
        resp = atacante.get(f'/cleaning_summary/{otra_trabajadora.id}')

        assert resp.status_code == 403


# ── Sesiones y jornada ────────────────────────────────────────────────────────

class TestJornada:

    def test_active_sessions_no_expone_las_de_otra(
        self, atacante, limpieza_ajena, otra_trabajadora
    ):
        resp = atacante.get(
            f'/api/worker/active-sessions?worker_id={otra_trabajadora.id}')

        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_active_session_no_expone_la_de_otra(
        self, atacante, limpieza_ajena, otra_trabajadora
    ):
        resp = atacante.get(
            f'/api/worker/active-session?worker_id={otra_trabajadora.id}')

        assert resp.status_code == 200
        assert resp.get_json()['active'] is False

    def test_today_no_expone_la_jornada_de_otra(
        self, atacante, otra_trabajadora, room, db
    ):
        ahora = datetime.now()
        db.session.add(CleaningRecord(
            cleaner_id=otra_trabajadora.id, room_id=room.id,
            start_time=ahora - timedelta(hours=1), end_time=ahora))
        db.session.commit()

        resp = atacante.get(f'/api/worker/today?worker_id={otra_trabajadora.id}')

        assert resp.status_code == 200
        assert resp.get_json()['sessions'] == []

    def test_my_groups_no_expone_los_grupos_de_otra(
        self, atacante, otra_trabajadora, db
    ):
        grupo = ResidentGroup(name='Planta 1', color='#0d6efd')
        db.session.add(grupo)
        otra_trabajadora.groups.append(grupo)
        db.session.commit()

        resp = atacante.get(f'/api/worker/my-groups?worker_id={otra_trabajadora.id}')

        assert resp.status_code == 200
        assert resp.get_json()['groups'] == []

    def test_identity_status_devuelve_el_estado_propio(
        self, atacante, cleaner_user, otra_trabajadora, db
    ):
        otra_trabajadora.identity_verified = True
        cleaner_user.identity_verified = False
        db.session.commit()

        resp = atacante.get(
            f'/api/worker/identity-status?worker_id={otra_trabajadora.id}')

        assert resp.status_code == 200
        assert resp.get_json()['verified'] is False


# ── Documentos y formación ────────────────────────────────────────────────────

class TestDocumentosYFormacion:

    def test_pending_documents_usa_las_firmas_propias(
        self, atacante, cleaner_user, otra_trabajadora, db
    ):
        doc = LegalDocument(title='Protocolo', content='texto', active=True)
        db.session.add(doc)
        db.session.flush()
        # El atacante YA lo firmó; la otra trabajadora no.
        db.session.add(DocumentSignature(document_id=doc.id,
                                         cleaner_id=cleaner_user.id))
        db.session.commit()

        resp = atacante.get(
            f'/api/worker/pending-documents?worker_id={otra_trabajadora.id}')

        assert resp.status_code == 200
        # Si hiciera caso al parámetro, el documento saldría como pendiente.
        assert resp.get_json() == []

    def test_pending_training_usa_la_formacion_propia(
        self, atacante, cleaner_user, otra_trabajadora, db
    ):
        pill = TrainingPill(title='Higiene de manos', active=True, assign_mode='all')
        db.session.add(pill)
        db.session.flush()
        db.session.add(TrainingCompletion(pill_id=pill.id, cleaner_id=cleaner_user.id,
                                          passed=True, score=100))
        db.session.commit()

        resp = atacante.get(
            f'/api/worker/pending-training?worker_id={otra_trabajadora.id}')

        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_video_complete_no_marca_la_sesion_de_otra(
        self, atacante, otra_trabajadora, db
    ):
        pill = TrainingPill(title='Movilizaciones', active=True)
        db.session.add(pill)
        db.session.flush()
        completion = TrainingCompletion(pill_id=pill.id,
                                        cleaner_id=otra_trabajadora.id,
                                        video_watched=False)
        db.session.add(completion)
        db.session.commit()

        resp = atacante.post(f'/api/worker/training/{pill.id}/video-complete',
                             json={'worker_id': otra_trabajadora.id})

        assert resp.status_code == 400          # el atacante no tiene sesión activa
        assert db.session.get(TrainingCompletion, completion.id).video_watched is False


# ── Sin token no se entra ─────────────────────────────────────────────────────

RUTAS_JWT = [
    '/api/worker/active-sessions',
    '/api/worker/active-session',
    '/api/worker/today',
    '/api/worker/my-groups',
    '/api/worker/identity-status',
    '/api/worker/pending-documents',
    '/api/worker/pending-training',
    '/check_cleaning',
    '/api/residents',
]


@pytest.mark.parametrize('ruta', RUTAS_JWT)
def test_sin_token_devuelve_401(client, db, ruta):
    assert client.get(ruta).status_code == 401
