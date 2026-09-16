"""
test_uso_aplicacion.py — Tests de la herramienta `uso_de_la_aplicacion` del chatbot.

Responde a preguntas del tipo "¿qué trabajadoras están usando menos la
aplicación?". Es una pregunta sobre el trabajo de personas concretas, asi que lo
que mas se prueba aqui es que NO se pueda contestar mal: que una baja, unas
vacaciones o un rol de gestion no hagan que alguien salga senalada sin motivo.
"""

import json
from datetime import datetime, timedelta, date

import pytest

from app.chatbot import _uso_de_la_aplicacion
from app.models import (Cleaner, CleaningRecord, CareRecord, WorkerDeviceLogin,
                        WorkerDevice, ShiftAssignment, Absence, AbsenceType,
                        Resident)


def crear(db, username, name, role='limpieza', admin=False):
    c = Cleaner(username=username, name=name, role=role, is_admin=admin)
    c.set_password('x')
    db.session.add(c)
    db.session.flush()
    return c


def turnos(db, worker, cuantos):
    """`cuantos` dias de turno consecutivos acabando ayer."""
    for i in range(1, cuantos + 1):
        db.session.add(ShiftAssignment(cleaner_id=worker.id,
                                       date=date.today() - timedelta(days=i)))


def limpiezas(db, worker, room, cuantas, dia_base=1):
    for i in range(cuantas):
        db.session.add(CleaningRecord(
            cleaner_id=worker.id, room_id=room.id,
            start_time=datetime.now() - timedelta(days=dia_base, hours=i)))


def por_nombre(salida, nombre):
    return next(t for t in salida['trabajadoras'] if t['nombre'] == nombre)


class TestUsoDeLaAplicacion:

    def test_cuenta_entradas_y_registros(self, db, room):
        ana = crear(db, 'ana', 'Ana Ruiz')
        dev = WorkerDevice(device_uid='a' * 20, first_seen=datetime.now(),
                           last_seen=datetime.now())
        db.session.add(dev)
        db.session.flush()
        db.session.add(WorkerDeviceLogin(device_id=dev.id, worker_id=ana.id,
                                         at=datetime.now() - timedelta(days=2)))
        turnos(db, ana, 5)
        limpiezas(db, ana, room, 3)
        db.session.commit()

        fila = por_nombre(json.loads(_uso_de_la_aplicacion(30)), 'Ana Ruiz')
        assert fila['entradas_en_la_webapp'] == 1
        assert fila['limpiezas_registradas'] == 3
        assert fila['registros_totales'] == 3
        assert fila['dias_con_turno'] == 5
        assert fila['registros_por_dia_con_turno'] == 0.6

    def test_ordena_de_menos_uso_a_mas(self, db, room):
        poco = crear(db, 'poco', 'Poco Uso')
        mucho = crear(db, 'mucho', 'Mucho Uso')
        turnos(db, poco, 10)
        turnos(db, mucho, 10)
        limpiezas(db, poco, room, 1)
        limpiezas(db, mucho, room, 20)
        db.session.commit()

        nombres = [t['nombre'] for t in json.loads(_uso_de_la_aplicacion(30))['trabajadoras']]
        assert nombres.index('Poco Uso') < nombres.index('Mucho Uso')

    def test_quien_no_tiene_turnos_queda_aparte(self, db, room):
        """Senalar a quien no ha trabajado seria acusarla de nada."""
        crear(db, 'nueva', 'Recien Contratada')
        con_turnos = crear(db, 'vieja', 'Con Turnos')
        turnos(db, con_turnos, 10)
        limpiezas(db, con_turnos, room, 1)
        db.session.commit()

        salida = json.loads(_uso_de_la_aplicacion(30))
        nueva = por_nombre(salida, 'Recien Contratada')
        assert nueva['uso_comparable'] is False
        assert 'turno' in nueva['motivo_no_comparable']
        assert nueva['registros_por_dia_con_turno'] is None
        # Y va despues de las que si se pueden comparar
        nombres = [t['nombre'] for t in salida['trabajadoras']]
        assert nombres.index('Con Turnos') < nombres.index('Recien Contratada')

    def test_la_primera_de_la_lista_es_la_respuesta(self, db, room):
        """Lo importante: quien encabeza la lista es a quien va a nombrar el
        chatbot, asi que no puede ser una gestora ni alguien de baja."""
        crear(db, 'coord', 'La Gestora', role='gestion', admin=True)
        tipo = AbsenceType(name='Vacaciones', short_name='V', color='#0069d9')
        db.session.add(tipo)
        db.session.flush()
        baja = crear(db, 'baja', 'De Vacaciones')
        turnos(db, baja, 10)
        db.session.add(Absence(cleaner_id=baja.id, absence_type_id=tipo.id,
                               start_date=date.today() - timedelta(days=10),
                               end_date=date.today() - timedelta(days=1)))
        floja = crear(db, 'floja', 'Usa Poco')
        turnos(db, floja, 10)
        limpiezas(db, floja, room, 1)
        mucha = crear(db, 'mucha', 'Usa Mucho')
        turnos(db, mucha, 10)
        limpiezas(db, mucha, room, 30)
        db.session.commit()

        salida = json.loads(_uso_de_la_aplicacion(30))
        assert salida['trabajadoras'][0]['nombre'] == 'Usa Poco'
        comparables = [t['nombre'] for t in salida['trabajadoras'] if t['uso_comparable']]
        assert comparables == ['Usa Poco', 'Usa Mucho']
        assert por_nombre(salida, 'La Gestora')['uso_comparable'] is False
        assert por_nombre(salida, 'De Vacaciones')['uso_comparable'] is False

    def test_las_ausencias_salen_para_poder_explicar_el_cero(self, db):
        """Una baja de dos semanas explica un cero. Sin este dato, no."""
        baja = crear(db, 'baja', 'De Baja')
        tipo = AbsenceType(name='Baja medica', short_name='BM', color='#cc0000')
        db.session.add(tipo)
        db.session.flush()
        db.session.add(Absence(cleaner_id=baja.id, absence_type_id=tipo.id,
                               start_date=date.today() - timedelta(days=14),
                               end_date=date.today() - timedelta(days=1)))
        turnos(db, baja, 14)
        db.session.commit()

        fila = por_nombre(json.loads(_uso_de_la_aplicacion(30)), 'De Baja')
        assert fila['dias_de_ausencia'] == 14
        assert fila['registros_totales'] == 0
        assert fila['uso_comparable'] is False
        assert 'ausente' in fila['motivo_no_comparable']

    def test_el_rol_y_admin_viajan_para_no_juzgar_a_gestion(self, db):
        gestora = crear(db, 'gestion', 'La Gestora', role='gestion', admin=True)
        db.session.commit()

        fila = por_nombre(json.loads(_uso_de_la_aplicacion(30)), 'La Gestora')
        assert fila['rol'] == 'gestion'
        assert fila['es_admin'] is True
        assert fila['uso_comparable'] is False
        assert 'panel' in fila['motivo_no_comparable']

    def test_solo_mira_el_periodo_pedido(self, db, room):
        ana = crear(db, 'ana', 'Ana Ruiz')
        turnos(db, ana, 3)
        limpiezas(db, ana, room, 2, dia_base=1)     # dentro
        limpiezas(db, ana, room, 5, dia_base=60)    # fuera
        db.session.commit()

        assert por_nombre(json.loads(_uso_de_la_aplicacion(7)),
                          'Ana Ruiz')['limpiezas_registradas'] == 2
        assert por_nombre(json.loads(_uso_de_la_aplicacion(90)),
                          'Ana Ruiz')['limpiezas_registradas'] == 7

    def test_cuenta_los_dias_distintos_no_los_registros(self, db, room):
        ana = crear(db, 'ana', 'Ana Ruiz')
        turnos(db, ana, 5)
        limpiezas(db, ana, room, 4, dia_base=1)     # 4 registros, 1 dia
        limpiezas(db, ana, room, 2, dia_base=3)     # 2 registros, otro dia
        db.session.commit()

        fila = por_nombre(json.loads(_uso_de_la_aplicacion(30)), 'Ana Ruiz')
        assert fila['limpiezas_registradas'] == 6
        assert fila['dias_distintos_con_registros'] == 2

    def test_las_dadas_de_baja_no_salen(self, db):
        fuera = crear(db, 'fuera', 'Ya No Trabaja')
        fuera.active = False
        db.session.commit()

        nombres = [t['nombre'] for t in json.loads(_uso_de_la_aplicacion(30))['trabajadoras']]
        assert 'Ya No Trabaja' not in nombres

    @pytest.mark.parametrize('dias', [0, -5, 9999])
    def test_el_periodo_se_acota(self, db, dias):
        salida = json.loads(_uso_de_la_aplicacion(dias))
        assert 1 <= salida['periodo_dias'] <= 365

    def test_lleva_las_instrucciones_para_no_acusar_a_nadie(self, db):
        """El aviso viaja con los datos: si se separa, se pierde."""
        salida = json.loads(_uso_de_la_aplicacion(30))
        assert 'uso_comparable' in salida['como_leerlo']
        assert 'motivo_no_comparable' in salida['como_leerlo']

    def test_sin_datos_no_revienta(self, db):
        salida = json.loads(_uso_de_la_aplicacion(30))
        assert salida['trabajadoras'] == []


class TestLaHerramientaEstaEnchufada:

    def test_esta_declarada_y_tiene_handler(self):
        from app.chatbot import TOOLS, _get_tool_handlers
        assert any(t['name'] == 'uso_de_la_aplicacion' for t in TOOLS)
        assert 'uso_de_la_aplicacion' in _get_tool_handlers(is_admin=False)

    def test_el_prompt_avisa_de_como_interpretarlo(self):
        from app.chatbot import SYSTEM_PROMPT
        assert 'uso_de_la_aplicacion' in SYSTEM_PROMPT
        assert 'uso_comparable' in SYSTEM_PROMPT
        assert 'motivo_no_comparable' in SYSTEM_PROMPT
