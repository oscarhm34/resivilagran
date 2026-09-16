"""
test_sin_atencion.py — Tests de las preguntas EN NEGATIVO del chatbot.

"¿A qué residentes no se les ha registrado cambio de pañal entre las 9 y las 13?"

Es una pregunta delicada: la aplicacion NO sabe que residentes necesitan cada
atencion, asi que una lista en crudo mezcla a quien se ha quedado sin ella con
quien nunca la ha necesitado. Lo que mas se prueba aqui es esa separacion.
"""

import json
from datetime import datetime, timedelta, time

import pytest

from app.chatbot import (_residentes_sin_atencion, _atenciones_por_tipo,
                         _hora, _franja)
from app.models import Resident, CareType, CareRecord, Cleaner


HOY = datetime.now().date()


def residente(db, nombre, hab, nfc):
    r = Resident(name=nombre, nfc_code=nfc, room_number=hab, active=True)
    db.session.add(r)
    db.session.flush()
    return r


def tipo_panal(db):
    ct = CareType(name='Cambio de pañal', active=True)
    db.session.add(ct)
    db.session.flush()
    return ct


def atencion(db, worker, res, ct, cuando):
    rec = CareRecord(worker_id=worker.id, resident_id=res.id,
                     care_type_id=ct.id, start_time=cuando,
                     end_time=cuando + timedelta(minutes=10))
    rec.care_types.append(ct)
    db.session.add(rec)
    return rec


def a_las(hora, minuto=0, dias_atras=0):
    return datetime.combine(HOY - timedelta(days=dias_atras), time(hora, minuto))


# ── Las horas, como las dice la gente ────────────────────────────────────────

class TestParseoDeHoras:

    @pytest.mark.parametrize('texto,esperado', [
        ('9', time(9, 0)), ('09', time(9, 0)), ('9:30', time(9, 30)),
        ('09:30', time(9, 30)), ('13', time(13, 0)), ('23:59', time(23, 59)),
        ('9h30', time(9, 30)),
    ])
    def test_entiende_la_hora_suelta(self, texto, esperado):
        """El modelo pasa "9" y "13", no "09:00"."""
        assert _hora(texto, None) == esperado

    @pytest.mark.parametrize('texto', [None, '', 'mediodia', '25', '9:99', 'abc'])
    def test_lo_que_no_entiende_cae_al_valor_por_defecto(self, texto):
        assert _hora(texto, 'DEFECTO') == 'DEFECTO'

    def test_una_franja_al_reves_se_ignora(self):
        """Pedir "de 13 a 9" no puede devolver cero registros en silencio."""
        inicio, fin, etiqueta = _franja(HOY, '13', '9')
        assert etiqueta == 'todo el día'
        assert fin > inicio

    def test_sin_horas_es_el_dia_entero(self):
        inicio, fin, etiqueta = _franja(HOY, None, None)
        assert etiqueta == 'todo el día'
        assert inicio.hour == 0 and (fin - inicio).days == 1


# ── La pregunta en negativo ──────────────────────────────────────────────────

class TestResidentesSinAtencion:

    @pytest.fixture
    def escenario(self, db, cleaner_user):
        """Tres residentes: uno atendido hoy, uno que se ha quedado sin ella,
        y uno que no la necesita (nunca la ha recibido)."""
        ct = tipo_panal(db)
        atendida = residente(db, 'ATENDIDA HOY', '101', 'nfc-1')
        olvidada = residente(db, 'OLVIDADA HOY', '102', 'nfc-2')
        continente = residente(db, 'NO LA NECESITA', '103', 'nfc-3')

        atencion(db, cleaner_user, atendida, ct, a_las(9, 48))
        # La olvidada la recibe todos los dias… hasta hoy.
        for d in range(1, 6):
            atencion(db, cleaner_user, olvidada, ct, a_las(10, 0, dias_atras=d))
        db.session.commit()
        return ct

    def test_separa_a_quien_la_necesita_de_quien_no(self, escenario):
        salida = json.loads(_residentes_sin_atencion('Cambio de pañal',
                                                     desde_hora='9', hasta_hora='13'))
        sin = salida['no_la_han_recibido']
        nombres = [r['residente'] for r in sin]

        assert 'ATENDIDA HOY' not in nombres           # si la ha recibido
        assert nombres[0] == 'OLVIDADA HOY'            # la primera, la que importa
        assert sin[0]['la_recibe_habitualmente'] is True
        assert sin[0]['horas_desde_la_ultima_vez'] is not None

        continente = next(r for r in sin if r['residente'] == 'NO LA NECESITA')
        assert continente['la_recibe_habitualmente'] is False
        assert continente['ultima_vez_que_la_recibio'] is None

    def test_la_franja_horaria_recorta_de_verdad(self, db, cleaner_user, escenario):
        """Una atencion a las 15:00 no cuenta si se pregunta de 9 a 13."""
        tarde = residente(db, 'ATENDIDA POR LA TARDE', '104', 'nfc-4')
        atencion(db, cleaner_user, tarde, escenario, a_las(15, 0))
        db.session.commit()

        manana = json.loads(_residentes_sin_atencion('Cambio de pañal',
                                                     desde_hora='9', hasta_hora='13'))
        assert 'ATENDIDA POR LA TARDE' in [r['residente'] for r in manana['no_la_han_recibido']]

        entero = json.loads(_residentes_sin_atencion('Cambio de pañal'))
        assert 'ATENDIDA POR LA TARDE' in [r['residente'] for r in entero['la_han_recibido']]

    def test_dice_la_franja_que_ha_mirado(self, escenario):
        salida = json.loads(_residentes_sin_atencion('Cambio de pañal',
                                                     desde_hora='9', hasta_hora='13'))
        assert salida['franja'] == 'de 09:00 a 13:00'
        assert salida['fecha'] == HOY.strftime('%d/%m/%Y')

    def test_cuenta_a_todos_los_residentes_activos(self, escenario):
        salida = json.loads(_residentes_sin_atencion('Cambio de pañal'))
        assert salida['total_residentes_activos'] == 3
        assert (len(salida['la_han_recibido']) +
                len(salida['no_la_han_recibido'])) == 3

    def test_los_dados_de_baja_no_salen(self, db, escenario):
        fuera = residente(db, 'YA NO ESTA', '105', 'nfc-5')
        fuera.active = False
        db.session.commit()

        salida = json.loads(_residentes_sin_atencion('Cambio de pañal'))
        assert 'YA NO ESTA' not in [r['residente'] for r in salida['no_la_han_recibido']]

    def test_lleva_el_aviso_de_como_leerlo(self, escenario):
        salida = json.loads(_residentes_sin_atencion('Cambio de pañal'))
        assert 'la_recibe_habitualmente' in salida['como_leerlo']
        assert 'no es una lista de fallos' in salida['como_leerlo']

    def test_un_tipo_que_no_existe_devuelve_los_que_si(self, escenario):
        salida = json.loads(_residentes_sin_atencion('Masaje tailandes'))
        assert 'error' in salida
        assert 'Cambio de pañal' in salida['tipos_disponibles']

    def test_sin_residentes_no_revienta(self, db):
        tipo_panal(db)
        db.session.commit()
        salida = json.loads(_residentes_sin_atencion('Cambio de pañal'))
        assert salida['no_la_han_recibido'] == []
        assert salida['total_residentes_activos'] == 0


class TestFranjaEnAtencionesPorTipo:

    def test_recorta_por_hora(self, db, cleaner_user):
        ct = tipo_panal(db)
        r = residente(db, 'UNA RESIDENTE', '201', 'nfc-9')
        atencion(db, cleaner_user, r, ct, a_las(9, 48))
        atencion(db, cleaner_user, r, ct, a_las(17, 0))
        db.session.commit()

        manana = json.loads(_atenciones_por_tipo('Cambio de pañal',
                                                 desde_hora='9', hasta_hora='13'))
        assert manana['total'] == 1
        assert manana['atenciones'][0]['hora'] == '09:48'
        # La respuesta tiene que decir que solo ha mirado esa franja
        assert manana['franja'] == 'de 09:00 a 13:00'

        todo = json.loads(_atenciones_por_tipo('Cambio de pañal'))
        assert todo['total'] == 2

    def test_sin_horas_se_comporta_como_antes(self, db, cleaner_user):
        ct = tipo_panal(db)
        r = residente(db, 'UNA RESIDENTE', '201', 'nfc-9')
        atencion(db, cleaner_user, r, ct, a_las(9, 48))
        db.session.commit()
        salida = json.loads(_atenciones_por_tipo('Cambio de pañal'))
        assert salida['total'] == 1
        assert salida['franja'] == 'todo el día'


class TestLaHerramientaEstaEnchufada:

    def test_declarada_y_con_handler(self):
        from app.chatbot import TOOLS, _get_tool_handlers
        assert any(t['name'] == 'residentes_sin_atencion' for t in TOOLS)
        assert 'residentes_sin_atencion' in _get_tool_handlers(is_admin=False)

    def test_atenciones_por_tipo_declara_la_franja(self):
        from app.chatbot import TOOLS
        herr = next(t for t in TOOLS if t['name'] == 'atenciones_por_tipo')
        props = herr['input_schema']['properties']
        assert 'desde_hora' in props and 'hasta_hora' in props

    def test_el_prompt_explica_la_pregunta_en_negativo(self):
        from app.chatbot import SYSTEM_PROMPT
        assert 'residentes_sin_atencion' in SYSTEM_PROMPT
        assert 'la_recibe_habitualmente' in SYSTEM_PROMPT
