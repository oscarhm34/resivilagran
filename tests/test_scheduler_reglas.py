"""
test_scheduler_reglas.py — Reglas laborales del generador de cuadrantes.

`app/scheduler.py` decide descanso mínimo entre turnos, horas semanales y días
consecutivos: son horas pagadas y cumplimiento laboral. La regla 05-tests pide
test unitario directo de estas funciones con los casos límite, y no había
ninguno.

Se prueban las funciones sin pasar por HTTP, como pide la regla.
"""

from datetime import date, time, timedelta

import pytest

from app.models import Cleaner, ShiftType
from app.scheduler import SmartScheduler


@pytest.fixture
def turnos(db):
    """Mañana, tarde y noche. La noche cruza la medianoche."""
    tipos = {
        'M': ShiftType(name='Mañana', short_name='M', color='#0069d9',
                       start_time=time(7, 0), end_time=time(15, 0), breaks_minutes=30),
        'T': ShiftType(name='Tarde', short_name='T', color='#1a7f37',
                       start_time=time(15, 0), end_time=time(23, 0), breaks_minutes=30),
        'N': ShiftType(name='Noche', short_name='N', color='#bf8700',
                       start_time=time(22, 0), end_time=time(6, 0), breaks_minutes=0),
    }
    db.session.add_all(tipos.values())
    db.session.commit()
    return tipos


@pytest.fixture
def trabajadora(db):
    c = Cleaner(username='fatima', name='Fatima Zahra', active=True)
    c.set_password('pass1234')
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture
def planificador(db, turnos, trabajadora, app):
    return SmartScheduler(2026, 9)


# ── Horas de un turno ────────────────────────────────────────────────────────

def test_las_horas_del_turno_descuentan_el_descanso(planificador, turnos):
    """De 07:00 a 15:00 con 30 minutos de descanso son 7,5 horas, no 8."""
    assert planificador._shift_hours(turnos['M'].id) == 7.5


def test_un_turno_de_noche_cruza_la_medianoche(planificador, turnos):
    """De 22:00 a 06:00 son 8 horas, no -16."""
    assert planificador._shift_hours(turnos['N'].id) == 8.0


# ── Descanso mínimo entre turnos ─────────────────────────────────────────────

def test_encadenar_noche_y_manana_incumple_el_descanso(planificador, turnos, trabajadora):
    """Salir a las 06:00 y entrar a las 07:00 del mismo día es 1 hora de descanso."""
    dia = date(2026, 9, 10)
    planificador.schedule[(trabajadora.id, dia - timedelta(days=1))] = turnos['N'].id

    assert planificador._check_rest(trabajadora.id, dia, turnos['M'].id) is False


def test_tarde_y_manana_del_dia_siguiente_tambien_incumple(planificador, turnos, trabajadora):
    """Salir a las 23:00 y entrar a las 07:00 son 8 horas: por debajo de 12."""
    dia = date(2026, 9, 10)
    planificador.schedule[(trabajadora.id, dia - timedelta(days=1))] = turnos['T'].id

    assert planificador._check_rest(trabajadora.id, dia, turnos['M'].id) is False


def test_manana_y_manana_respeta_el_descanso(planificador, turnos, trabajadora):
    """Salir a las 15:00 y entrar a las 07:00 del día siguiente son 16 horas."""
    dia = date(2026, 9, 10)
    planificador.schedule[(trabajadora.id, dia - timedelta(days=1))] = turnos['M'].id

    assert planificador._check_rest(trabajadora.id, dia, turnos['M'].id) is True


def test_el_descanso_tambien_se_mira_hacia_adelante(planificador, turnos, trabajadora):
    """Poner una mañana el día siguiente de una tarde también incumple."""
    dia = date(2026, 9, 10)
    planificador.schedule[(trabajadora.id, dia + timedelta(days=1))] = turnos['M'].id

    assert planificador._check_rest(trabajadora.id, dia, turnos['T'].id) is False


def test_sin_turnos_alrededor_el_descanso_se_cumple(planificador, turnos, trabajadora):
    assert planificador._check_rest(trabajadora.id, date(2026, 9, 10), turnos['M'].id) is True


# ── Horas semanales ──────────────────────────────────────────────────────────

def test_las_horas_semanales_suman_de_lunes_a_domingo(planificador, turnos, trabajadora):
    lunes = date(2026, 9, 7)
    assert lunes.weekday() == 0
    for offset in range(5):                       # cinco mañanas de 7,5 h
        planificador.schedule[(trabajadora.id, lunes + timedelta(days=offset))] = turnos['M'].id

    assert planificador._week_hours(trabajadora.id, lunes) == 37.5


def test_las_horas_semanales_no_cuentan_la_semana_de_al_lado(planificador, turnos, trabajadora):
    """El domingo anterior no entra en la semana del lunes."""
    lunes = date(2026, 9, 7)
    planificador.schedule[(trabajadora.id, lunes - timedelta(days=1))] = turnos['M'].id

    assert planificador._week_hours(trabajadora.id, lunes) == 0.0


def test_week_hours_with_anade_el_turno_que_se_esta_probando(planificador, turnos, trabajadora):
    lunes = date(2026, 9, 7)
    planificador.schedule[(trabajadora.id, lunes)] = turnos['M'].id

    assert planificador._week_hours_with(trabajadora.id, lunes, turnos['T'].id) == 15.0


# ── Días consecutivos ────────────────────────────────────────────────────────

def test_cuenta_los_dias_seguidos_trabajados(planificador, turnos, trabajadora):
    dia = date(2026, 9, 10)
    for offset in range(1, 4):
        planificador.schedule[(trabajadora.id, dia - timedelta(days=offset))] = turnos['M'].id

    assert planificador._consecutive_days(trabajadora.id, dia) == 3


def test_un_dia_libre_corta_la_racha(planificador, turnos, trabajadora):
    dia = date(2026, 9, 10)
    planificador.schedule[(trabajadora.id, dia - timedelta(days=1))] = turnos['M'].id
    # el dia -2 queda libre
    planificador.schedule[(trabajadora.id, dia - timedelta(days=3))] = turnos['M'].id

    assert planificador._consecutive_days(trabajadora.id, dia) == 1


def test_una_ausencia_tambien_corta_la_racha(planificador, turnos, trabajadora):
    """Una baja no puede contar como día trabajado."""
    dia = date(2026, 9, 10)
    planificador.schedule[(trabajadora.id, dia - timedelta(days=1))] = turnos['M'].id
    planificador.absent_days.add((trabajadora.id, dia - timedelta(days=2)))
    planificador.schedule[(trabajadora.id, dia - timedelta(days=3))] = turnos['M'].id

    assert planificador._consecutive_days(trabajadora.id, dia) == 1


def test_sin_dias_previos_la_racha_es_cero(planificador, trabajadora):
    assert planificador._consecutive_days(trabajadora.id, date(2026, 9, 10)) == 0
