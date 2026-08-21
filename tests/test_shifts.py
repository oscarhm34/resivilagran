"""
test_shifts.py — Tipos de turno (ShiftType) y API de turnos de la trabajadora.

Cubre el bug que devolvia 500 al crear un tipo de turno sin rellenar las horas:
un <input type="time"> vacio envia '', asi que el default de request.form.get()
nunca se aplicaba y "".split(':') reventaba con ValueError.
"""

from datetime import date, time

import pytest

from app.models import (
    AuditLog, RotationPattern, RotationPatternDay, ShiftAssignment, ShiftType,
)
from app.utils import _parse_hhmm


# ── Fixtures locales ──────────────────────────────────────────────────────────

@pytest.fixture
def shift_type(db):
    """Tipo de turno de manana ya existente."""
    st = ShiftType(name='Manana', short_name='M', color='#0d6efd',
                   start_time=time(7, 0), end_time=time(15, 0),
                   breaks_minutes=30, sort_order=1, active=True)
    db.session.add(st)
    db.session.commit()
    return st


def _form(**overrides):
    """Formulario valido de tipo de turno, con los campos que se quieran pisar."""
    data = {
        'shift_type_id': '',
        'name': 'Tarde',
        'short_name': 'T',
        'color': '#cf222e',
        'start_time': '15:00',
        'end_time': '22:00',
        'breaks_minutes': '15',
        'sort_order': '2',
    }
    data.update(overrides)
    return data


# ── Autorizacion ──────────────────────────────────────────────────────────────

class TestAutorizacion:

    def test_listado_sin_sesion_redirige_al_login(self, client):
        resp = client.get('/cuadrantes/manage-shift-types')

        assert resp.status_code == 302
        assert '/admin/login' in resp.headers['Location']

    def test_crear_sin_sesion_no_crea_nada(self, client, db):
        resp = client.post('/shift-types/add_edit', data=_form())

        assert resp.status_code == 302
        assert ShiftType.query.count() == 0

    def test_login_admin_rechaza_a_quien_no_es_admin(self, client, cleaner_user, db):
        client.post('/admin/login',
                    data={'username': 'limpiadora1', 'password': 'limpia123'},
                    follow_redirects=True)

        resp = client.post('/shift-types/add_edit', data=_form())

        assert resp.status_code == 302          # sin sesion: redirige al login
        assert ShiftType.query.count() == 0

    def test_usuario_logueado_sin_is_admin_recibe_403(self, auth_client, admin_user, db):
        admin_user.is_admin = False
        db.session.commit()

        resp = auth_client.post('/shift-types/add_edit', data=_form())

        assert resp.status_code == 403
        assert ShiftType.query.count() == 0

    def test_borrar_sin_sesion_no_borra(self, client, shift_type, db):
        resp = client.post(f'/shift-types/delete/{shift_type.id}')

        assert resp.status_code == 302
        assert db.session.get(ShiftType, shift_type.id) is not None

    def test_toggle_sin_sesion_no_cambia_estado(self, client, shift_type, db):
        resp = client.post('/shift-types/toggle-active',
                           json={'id': shift_type.id, 'active': False})

        assert resp.status_code == 302
        assert db.session.get(ShiftType, shift_type.id).active is True


# ── Camino correcto ───────────────────────────────────────────────────────────

class TestCrearTipoTurno:

    def test_crea_el_tipo_con_todos_sus_campos(self, auth_client, db):
        resp = auth_client.post('/shift-types/add_edit', data=_form())

        assert resp.status_code == 302
        st = ShiftType.query.filter_by(name='Tarde').one()
        assert st.short_name == 'T'
        assert st.color == '#cf222e'
        assert st.start_time == time(15, 0)
        assert st.end_time == time(22, 0)
        assert st.breaks_minutes == 15
        assert st.sort_order == 2
        assert st.active is True

    def test_registra_auditoria(self, auth_client, db):
        auth_client.post('/shift-types/add_edit', data=_form())

        log = AuditLog.query.filter_by(table_name='shift_type', action='create').one()
        assert log.record_id == ShiftType.query.one().id

    def test_acepta_turno_de_noche_que_cruza_medianoche(self, auth_client, db):
        resp = auth_client.post('/shift-types/add_edit', data=_form(
            name='Noche', short_name='N', start_time='22:00', end_time='06:00'))

        assert resp.status_code == 302
        st = ShiftType.query.filter_by(name='Noche').one()
        assert st.start_time == time(22, 0)
        assert st.end_time == time(6, 0)

    def test_enteros_vacios_se_normalizan_a_cero(self, auth_client, db):
        auth_client.post('/shift-types/add_edit',
                         data=_form(breaks_minutes='', sort_order=''))

        st = ShiftType.query.one()
        assert st.breaks_minutes == 0
        assert st.sort_order == 0

    def test_color_invalido_cae_al_color_por_defecto(self, auth_client, db):
        auth_client.post('/shift-types/add_edit', data=_form(color='javascript:x'))

        assert ShiftType.query.one().color == '#0d6efd'


# ── Entrada invalida (regresion del error 500) ───────────────────────────────

class TestEntradaInvalida:

    def test_horas_vacias_no_provocan_error_500(self, auth_client, db):
        """Regresion: '' .split(':') reventaba con ValueError -> 500."""
        resp = auth_client.post('/shift-types/add_edit',
                                data=_form(start_time='', end_time=''))

        assert resp.status_code == 302
        assert ShiftType.query.count() == 0

    @pytest.mark.parametrize('valor', ['07', 'abc', '99:99', '24:00', '-1:00', ':'])
    def test_hora_mal_formada_no_provoca_error_500(self, auth_client, db, valor):
        resp = auth_client.post('/shift-types/add_edit', data=_form(start_time=valor))

        assert resp.status_code == 302
        assert ShiftType.query.count() == 0

    def test_hora_con_segundos_es_valida(self, auth_client, db):
        """Lo que renderiza str(datetime.time) al editar: 'HH:MM:SS'."""
        resp = auth_client.post('/shift-types/add_edit',
                                data=_form(start_time='15:00:00', end_time='22:00:00'))

        assert resp.status_code == 302
        assert ShiftType.query.one().start_time == time(15, 0)

    def test_nombre_vacio_no_crea_nada(self, auth_client, db):
        auth_client.post('/shift-types/add_edit', data=_form(name='   '))

        assert ShiftType.query.count() == 0

    def test_nombre_demasiado_largo_no_crea_nada(self, auth_client, db):
        auth_client.post('/shift-types/add_edit', data=_form(name='x' * 60))

        assert ShiftType.query.count() == 0

    def test_nombre_duplicado_no_provoca_error_500(self, auth_client, shift_type, db):
        resp = auth_client.post('/shift-types/add_edit', data=_form(name='Manana'))

        assert resp.status_code == 302
        assert ShiftType.query.count() == 1

    def test_nombre_duplicado_ignora_mayusculas(self, auth_client, shift_type, db):
        auth_client.post('/shift-types/add_edit', data=_form(name='MANANA'))

        assert ShiftType.query.count() == 1

    def test_hora_fin_igual_a_inicio_se_rechaza(self, auth_client, db):
        """Con end == start, net_hours lo contaria como un turno de 24 h."""
        auth_client.post('/shift-types/add_edit',
                         data=_form(start_time='07:00', end_time='07:00'))

        assert ShiftType.query.count() == 0

    def test_descanso_mayor_que_el_turno_se_rechaza(self, auth_client, db):
        auth_client.post('/shift-types/add_edit',
                         data=_form(start_time='07:00', end_time='15:00',
                                    breaks_minutes='600'))

        assert ShiftType.query.count() == 0

    def test_id_no_numerico_no_provoca_error_500(self, auth_client, db):
        resp = auth_client.post('/shift-types/add_edit',
                                data=_form(shift_type_id='abc'))

        assert resp.status_code == 302
        assert ShiftType.query.count() == 0

    def test_id_inexistente_no_crea_uno_nuevo(self, auth_client, db):
        resp = auth_client.post('/shift-types/add_edit',
                                data=_form(shift_type_id='9999'))

        assert resp.status_code == 302
        assert ShiftType.query.count() == 0


# ── Edicion ───────────────────────────────────────────────────────────────────

class TestEdicion:

    def test_actualiza_los_campos(self, auth_client, shift_type, db):
        resp = auth_client.post('/shift-types/add_edit', data=_form(
            shift_type_id=str(shift_type.id), name='Manana larga',
            short_name='ML', start_time='06:30', end_time='14:30'))

        assert resp.status_code == 302
        st = db.session.get(ShiftType, shift_type.id)
        assert st.name == 'Manana larga'
        assert st.start_time == time(6, 30)
        assert ShiftType.query.count() == 1

    def test_editar_no_pisa_el_estado_activo(self, auth_client, shift_type, db):
        shift_type.active = False
        db.session.commit()

        auth_client.post('/shift-types/add_edit',
                         data=_form(shift_type_id=str(shift_type.id), name='Manana'))

        assert db.session.get(ShiftType, shift_type.id).active is False

    def test_conservar_el_propio_nombre_no_es_duplicado(self, auth_client, shift_type, db):
        resp = auth_client.post('/shift-types/add_edit', data=_form(
            shift_type_id=str(shift_type.id), name='Manana', short_name='MA'))

        assert resp.status_code == 302
        assert db.session.get(ShiftType, shift_type.id).short_name == 'MA'


# ── Borrado ───────────────────────────────────────────────────────────────────

class TestBorrado:

    def test_borra_si_no_tiene_referencias(self, auth_client, shift_type, db):
        st_id = shift_type.id

        resp = auth_client.post(f'/shift-types/delete/{st_id}')

        assert resp.status_code == 302
        assert db.session.get(ShiftType, st_id) is None
        assert AuditLog.query.filter_by(table_name='shift_type', action='delete').count() == 1

    def test_con_asignaciones_no_borra(self, auth_client, shift_type, cleaner_user, db):
        db.session.add(ShiftAssignment(cleaner_id=cleaner_user.id,
                                       date=date(2026, 8, 21),
                                       shift_type_id=shift_type.id))
        db.session.commit()

        resp = auth_client.post(f'/shift-types/delete/{shift_type.id}')

        assert resp.status_code == 302
        assert db.session.get(ShiftType, shift_type.id) is not None

    def test_con_patron_de_rotacion_no_provoca_error_500(self, auth_client, shift_type, db):
        """Antes reventaba con IntegrityError: solo se miraba `assignments`."""
        pattern = RotationPattern(name='7x7', cycle_days=7)
        db.session.add(pattern)
        db.session.flush()
        db.session.add(RotationPatternDay(pattern_id=pattern.id, day_number=0,
                                          shift_type_id=shift_type.id))
        db.session.commit()

        resp = auth_client.post(f'/shift-types/delete/{shift_type.id}')

        assert resp.status_code == 302
        assert db.session.get(ShiftType, shift_type.id) is not None

    def test_id_inexistente_no_provoca_error_500(self, auth_client, db):
        resp = auth_client.post('/shift-types/delete/9999')

        assert resp.status_code == 302


# ── Toggle activo ─────────────────────────────────────────────────────────────

class TestToggleActive:

    def test_desactiva_el_tipo(self, auth_client, shift_type, db):
        resp = auth_client.post('/shift-types/toggle-active',
                                json={'id': shift_type.id, 'active': False})

        assert resp.status_code == 200
        assert resp.get_json() == {'ok': True, 'active': False}
        assert db.session.get(ShiftType, shift_type.id).active is False

    def test_id_inexistente_devuelve_404(self, auth_client, db):
        resp = auth_client.post('/shift-types/toggle-active',
                                json={'id': 9999, 'active': True})

        assert resp.status_code == 404

    def test_id_no_numerico_devuelve_400(self, auth_client, db):
        resp = auth_client.post('/shift-types/toggle-active',
                                json={'id': 'abc', 'active': True})

        assert resp.status_code == 400

    def test_cuerpo_vacio_devuelve_400(self, auth_client, db):
        resp = auth_client.post('/shift-types/toggle-active',
                                data='', content_type='application/json')

        assert resp.status_code == 400


# ── Patrones de rotacion ──────────────────────────────────────────────────────

class TestPatrones:

    def test_dias_de_ciclo_vacios_no_provocan_error_500(self, auth_client, db):
        """Antes: type=int con default '7' devolvia el str '7' -> '7' < 1 -> TypeError."""
        resp = auth_client.post('/cuadrantes/patrones/add_edit',
                                data={'pattern_id': '', 'name': 'Ciclo',
                                      'description': '', 'cycle_days': ''})

        assert resp.status_code == 302
        assert RotationPattern.query.count() == 0

    def test_dias_de_ciclo_fuera_de_rango_se_rechazan(self, auth_client, db):
        auth_client.post('/cuadrantes/patrones/add_edit',
                         data={'pattern_id': '', 'name': 'Ciclo',
                               'description': '', 'cycle_days': '400'})

        assert RotationPattern.query.count() == 0

    def test_crea_el_patron_con_sus_dias(self, auth_client, db):
        resp = auth_client.post('/cuadrantes/patrones/add_edit',
                                data={'pattern_id': '', 'name': '3x3',
                                      'description': 'Tres y tres', 'cycle_days': '3'})

        assert resp.status_code == 302
        pattern = RotationPattern.query.one()
        assert pattern.cycle_days == 3
        assert RotationPatternDay.query.filter_by(pattern_id=pattern.id).count() == 3


# ── Validacion de cumplimiento laboral ────────────────────────────────────────

class TestValidacionCuadrante:

    def test_sin_parametros_no_provoca_error_500(self, auth_client, db):
        """Antes: monthrange(None, None) -> TypeError -> 500."""
        resp = auth_client.get('/cuadrantes/validate')

        assert resp.status_code == 200

    def test_mes_fuera_de_rango_devuelve_400(self, auth_client, db):
        resp = auth_client.get('/cuadrantes/validate?year=2026&month=13')

        assert resp.status_code == 400


# ── API de la trabajadora ─────────────────────────────────────────────────────

class TestApiMisTurnos:

    @staticmethod
    def _token(client, username, password):
        resp = client.post('/login', json={'username': username, 'password': password})
        assert resp.status_code == 200
        return resp.get_json()['access_token']

    def test_sin_token_devuelve_401(self, client, db):
        assert client.get('/api/worker/my-shifts').status_code == 401

    def test_devuelve_solo_los_turnos_propios(self, client, cleaner_user, admin_user,
                                              shift_type, db):
        hoy = date.today()
        db.session.add_all([
            ShiftAssignment(cleaner_id=cleaner_user.id, date=hoy,
                            shift_type_id=shift_type.id),
            ShiftAssignment(cleaner_id=admin_user.id, date=hoy,
                            shift_type_id=shift_type.id),
        ])
        db.session.commit()
        token = self._token(client, 'limpiadora1', 'limpia123')

        resp = client.get(f'/api/worker/my-shifts?month={hoy:%Y-%m}',
                          headers={'Authorization': f'Bearer {token}'})

        assert resp.status_code == 200
        turnos = [s for s in resp.get_json()['shifts'] if s.get('shift')]
        assert len(turnos) == 1
        assert turnos[0]['shift']['short_name'] == 'M'

    def test_mes_fuera_de_rango_devuelve_400(self, client, cleaner_user, db):
        """Antes: monthrange(2026, 13) -> IllegalMonthError -> 500."""
        token = self._token(client, 'limpiadora1', 'limpia123')

        resp = client.get('/api/worker/my-shifts?month=2026-13',
                          headers={'Authorization': f'Bearer {token}'})

        assert resp.status_code == 400

    def test_mes_mal_formado_devuelve_400(self, client, cleaner_user, db):
        token = self._token(client, 'limpiadora1', 'limpia123')

        resp = client.get('/api/worker/my-shifts?month=basura',
                          headers={'Authorization': f'Bearer {token}'})

        assert resp.status_code == 400


# ── Helper _parse_hhmm ────────────────────────────────────────────────────────

class TestParseHhmm:

    @pytest.mark.parametrize('valor, esperado', [
        ('07:00', time(7, 0)),
        ('07:00:00', time(7, 0)),
        (' 23:59 ', time(23, 59)),
        ('7:5', time(7, 5)),
        ('00:00', time(0, 0)),
    ])
    def test_valores_validos(self, valor, esperado):
        assert _parse_hhmm(valor) == esperado

    @pytest.mark.parametrize('valor', [
        '', None, '07', 'abc', '24:00', '07:60', '-1:00', ':', 'aa:bb', [],
    ])
    def test_valores_invalidos_devuelven_el_default(self, valor):
        assert _parse_hhmm(valor) is None
        assert _parse_hhmm(valor, time(9, 0)) == time(9, 0)
