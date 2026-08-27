"""
test_notificaciones_sesiones.py — Avisos de sesión abierta y su destino.

Cuando una limpieza o una atención se queda abierta, el aviso debe poder
pulsarse y llevar al listado donde están los botones de cerrar y eliminar.

Cubre:
- Filtro `estado` (abiertas / cerradas) en los dos listados y en sus exports.
- El aviso `stale_session_worker` enlaza al listado filtrado, anclado en la fila.
- La deduplicación funciona por enlace, sin ensuciar el campo `message`.
- El aviso agregado `stale_session` (>24h) ya no se genera.
- Borrado de registros de limpieza, con auditoría y control de acceso.
"""

from datetime import datetime, timedelta
from io import BytesIO

import pytest

from app.models import (AppSetting, AuditLog, CareRecord, CleaningRecord,
                        Notification, Resident)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def resident(db):
    r = Resident(name="Rosa Martínez", nfc_code="NFC-NOTIF-001", active=True)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture()
def limpieza_abierta(db, cleaner_user, room):
    """Limpieza sin cerrar, empezada hace tres horas (pasa el umbral por defecto)."""
    rec = CleaningRecord(
        cleaner_id=cleaner_user.id, room_id=room.id,
        start_time=datetime.now() - timedelta(hours=3), end_time=None,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


@pytest.fixture()
def atencion_abierta(db, cleaner_user, resident):
    rec = CareRecord(
        worker_id=cleaner_user.id, resident_id=resident.id,
        start_time=datetime.now() - timedelta(hours=3), end_time=None,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def _genera(app):
    """Lanza el generador de avisos dentro del contexto de la aplicación."""
    from app.blueprints.notifications import _generate_notifications
    with app.test_request_context():
        return _generate_notifications()


# ── Filtro «en curso» en los listados ────────────────────────────────────────

class TestFiltroEstadoLimpiezas:

    def test_abiertas_solo_devuelve_las_no_cerradas(
            self, auth_client, limpieza_abierta, completed_record):
        resp = auth_client.get('/registros-limpieza?estado=abiertas')

        assert resp.status_code == 200
        html = resp.data.decode('utf-8')
        assert f'id="rec-{limpieza_abierta.id}"' in html
        assert f'id="rec-{completed_record.id}"' not in html

    def test_cerradas_solo_devuelve_las_finalizadas(
            self, auth_client, limpieza_abierta, completed_record):
        resp = auth_client.get('/registros-limpieza?estado=cerradas')

        html = resp.data.decode('utf-8')
        assert f'id="rec-{completed_record.id}"' in html
        assert f'id="rec-{limpieza_abierta.id}"' not in html

    def test_sin_estado_devuelve_todas(
            self, auth_client, limpieza_abierta, completed_record):
        resp = auth_client.get('/registros-limpieza')

        html = resp.data.decode('utf-8')
        assert f'id="rec-{limpieza_abierta.id}"' in html
        assert f'id="rec-{completed_record.id}"' in html

    def test_el_export_respeta_el_filtro(
            self, auth_client, limpieza_abierta, completed_record):
        """Exportar desde la vista «En curso» no debe llevarse todo el historico."""
        # Estan los dos en requirements.txt; si faltan en el entorno local, el
        # fallo seria del entorno y no del codigo.
        pytest.importorskip('xlsxwriter')   # lo usa la ruta para escribir
        pytest.importorskip('openpyxl')     # lo usa pandas para leer
        import pandas as pd

        resp = auth_client.get('/exportar_excel?estado=abiertas')

        assert resp.status_code == 200
        df = pd.read_excel(BytesIO(resp.data))
        assert len(df) == 1


    def test_el_boton_de_exportar_arrastra_el_filtro(
            self, auth_client, limpieza_abierta):
        """Sin esto, exportar desde «En curso» se llevaria todo sin avisar."""
        html = auth_client.get(
            '/registros-limpieza?estado=abiertas').data.decode('utf-8')

        assert 'exportar_excel' in html
        assert 'estado=abiertas' in html.split('exportar_excel')[1][:300]


class TestFiltroEstadoAtenciones:

    def test_abiertas_solo_devuelve_las_no_cerradas(
            self, auth_client, atencion_abierta, db, cleaner_user, resident):
        cerrada = CareRecord(
            worker_id=cleaner_user.id, resident_id=resident.id,
            start_time=datetime.now() - timedelta(hours=5),
            end_time=datetime.now() - timedelta(hours=4),
        )
        db.session.add(cerrada)
        db.session.commit()

        html = auth_client.get(
            '/registros-atencion?estado=abiertas').data.decode('utf-8')

        assert f'id="rec-{atencion_abierta.id}"' in html
        assert f'id="rec-{cerrada.id}"' not in html


# ── El aviso lleva a donde se puede actuar ───────────────────────────────────

class TestEnlaceDelAviso:

    def test_limpieza_abierta_genera_aviso_con_enlace_al_listado(
            self, app, db, limpieza_abierta):
        _genera(app)

        aviso = Notification.query.filter_by(type='stale_session_worker').first()
        assert aviso is not None
        assert aviso.link == (
            f'/registros-limpieza?estado=abiertas#rec-{limpieza_abierta.id}')

    def test_atencion_abierta_genera_aviso_con_enlace_al_listado(
            self, app, db, atencion_abierta):
        _genera(app)

        aviso = Notification.query.filter_by(type='stale_session_worker').first()
        assert aviso is not None
        assert aviso.link == (
            f'/registros-atencion?estado=abiertas#rec-{atencion_abierta.id}')

    def test_el_aviso_no_usa_message_como_clave_interna(
            self, app, db, limpieza_abierta):
        """`message` hacia que la fila se pintara como desplegable, no como enlace."""
        _genera(app)

        aviso = Notification.query.filter_by(type='stale_session_worker').first()
        assert not aviso.message

    def test_generar_dos_veces_no_duplica_el_aviso(
            self, app, db, limpieza_abierta):
        _genera(app)
        _genera(app)

        assert Notification.query.filter_by(
            type='stale_session_worker').count() == 1

    def test_el_aviso_es_un_enlace_en_el_panel(
            self, app, db, auth_client, limpieza_abierta):
        _genera(app)

        html = auth_client.get('/admin/notifications').data.decode('utf-8')

        enlace = f'/registros-limpieza?estado=abiertas#rec-{limpieza_abierta.id}'
        assert f'<a href="{enlace}"' in html
        assert 'stale_clean_' not in html

    def test_ya_no_se_genera_el_aviso_agregado_de_24h(self, app, db,
                                                      cleaner_user, room):
        """La portada autocierra lo que pasa de 24h, asi que ese aviso era ruido."""
        vieja = CleaningRecord(
            cleaner_id=cleaner_user.id, room_id=room.id,
            start_time=datetime.now() - timedelta(hours=30), end_time=None,
        )
        db.session.add(vieja)
        db.session.commit()

        _genera(app)

        assert Notification.query.filter_by(type='stale_session').count() == 0

    def test_el_umbral_sale_de_la_configuracion(self, app, db, cleaner_user, room):
        AppSetting.set('session_max_minutes', '600')
        reciente = CleaningRecord(
            cleaner_id=cleaner_user.id, room_id=room.id,
            start_time=datetime.now() - timedelta(hours=3), end_time=None,
        )
        db.session.add(reciente)
        db.session.commit()

        _genera(app)

        assert Notification.query.filter_by(
            type='stale_session_worker').count() == 0


# ── Borrado de registros de limpieza ─────────────────────────────────────────

class TestBorrarRegistroDeLimpieza:

    def test_borra_el_registro(self, auth_client, db, completed_record):
        record_id = completed_record.id

        resp = auth_client.post(
            f'/admin/cleaning-record/{record_id}/delete', follow_redirects=True)

        assert resp.status_code == 200
        assert db.session.get(CleaningRecord, record_id) is None

    def test_deja_entrada_de_auditoria(self, auth_client, db, completed_record):
        record_id = completed_record.id

        auth_client.post(f'/admin/cleaning-record/{record_id}/delete',
                         follow_redirects=True)

        entrada = AuditLog.query.filter_by(
            action='delete', table_name='cleaning_record',
            record_id=record_id).first()
        assert entrada is not None
        assert entrada.user_id is not None

    def test_registro_inexistente_avisa_sin_reventar(self, auth_client):
        resp = auth_client.post('/admin/cleaning-record/99999/delete',
                                follow_redirects=True)

        assert resp.status_code == 200
        assert 'Registro no encontrado' in resp.data.decode('utf-8')

    def test_sin_sesion_no_se_puede_borrar(self, client, db, completed_record):
        record_id = completed_record.id

        resp = client.post(f'/admin/cleaning-record/{record_id}/delete')

        assert resp.status_code in (302, 401, 403)
        assert db.session.get(CleaningRecord, record_id) is not None
