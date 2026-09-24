"""
test_filtro_horario.py — Filtro por franja horaria en los registros de atencion.

Lo que se protege aqui: que la franja se aplique **a cada dia** del rango y no
como un intervalo continuo, y sobre todo que el turno de noche (22:00 a 06:00),
que cruza la medianoche, no salga vacio. Es el caso de uso que motivo el filtro
y el unico que un `between` ingenuo se come entero.

Se prueba tambien que el Excel filtre exactamente lo mismo que la pantalla: el
bloque de filtros estuvo duplicado y es facil que se separen otra vez.
"""

from datetime import datetime

import pytest

from app.models import CareRecord, Resident
from app.utils import _filtro_fecha_hora


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def residente(db):
    r = Resident(name='Antonia Vidal', nfc_code='RES-HOR-1', active=True)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def atenciones(db, residente, admin_user):
    """Una atencion cada pocas horas, del 1 al 3 de septiembre de 2026.

    Se guarda la hora en las notas para poder identificarlas en las aserciones.
    """
    momentos = [
        datetime(2026, 9, 1, 3, 0), datetime(2026, 9, 1, 9, 30),
        datetime(2026, 9, 1, 13, 0), datetime(2026, 9, 1, 23, 0),
        datetime(2026, 9, 2, 5, 0), datetime(2026, 9, 2, 10, 0),
        datetime(2026, 9, 2, 22, 30),
        datetime(2026, 9, 3, 8, 0), datetime(2026, 9, 3, 16, 0),
    ]
    for m in momentos:
        db.session.add(CareRecord(worker_id=admin_user.id, resident_id=residente.id,
                                  start_time=m, notes=m.strftime('%d %H:%M')))
    db.session.commit()
    return momentos


def _horas(consulta):
    """Las horas (HH:MM) que devuelve una consulta, ordenadas."""
    return sorted(r.start_time.strftime('%d %H:%M') for r in consulta.all())


# ── La funcion, sin pasar por HTTP ────────────────────────────────────────────

def test_franja_de_dia_dentro_del_mismo_dia(db, atenciones):
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  '2026-09-01', '2026-09-03', '08:00', '14:00')

    assert _horas(consulta) == ['01 09:30', '01 13:00', '02 10:00', '03 08:00']


def test_la_franja_de_noche_cruza_la_medianoche(db, atenciones):
    """22:00-06:00 es el turno de noche: la union de los dos extremos del dia."""
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  '2026-09-01', '2026-09-03', '22:00', '06:00')

    assert _horas(consulta) == ['01 03:00', '01 23:00', '02 05:00', '02 22:30']


def test_la_franja_se_aplica_a_cada_dia_no_como_intervalo_continuo(db, atenciones):
    """Si fuera un intervalo seguido del dia 1 al 3, saldria casi todo."""
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  '2026-09-01', '2026-09-03', '09:00', '11:00')

    assert _horas(consulta) == ['01 09:30', '02 10:00']


def test_solo_hora_de_inicio(db, atenciones):
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  '2026-09-01', '2026-09-01', '13:00', '')

    assert _horas(consulta) == ['01 13:00', '01 23:00']


def test_solo_hora_de_fin(db, atenciones):
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  '2026-09-01', '2026-09-01', '', '09:30')

    assert _horas(consulta) == ['01 03:00', '01 09:30']


def test_sin_horas_solo_filtra_por_dias(db, atenciones):
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  '2026-09-02', '2026-09-02')

    assert _horas(consulta) == ['02 05:00', '02 10:00', '02 22:30']


def test_el_dia_final_entra_entero(db, atenciones):
    """El rango es medianoche a medianoche del dia siguiente: el ultimo dia cuenta."""
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  '2026-09-03', '2026-09-03')

    assert _horas(consulta) == ['03 08:00', '03 16:00']


def test_una_hora_mal_escrita_se_ignora_y_no_rompe(db, atenciones):
    """Antes una fecha mal formada daba un 500: los parsers no levantan nunca."""
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  '2026-09-02', '2026-09-02', '99:99', 'manzana')

    assert _horas(consulta) == ['02 05:00', '02 10:00', '02 22:30']


def test_una_fecha_mal_escrita_se_ignora_y_no_rompe(db, atenciones):
    consulta = _filtro_fecha_hora(CareRecord.query, CareRecord.start_time,
                                  'ayer', '', '08:00', '14:00')

    assert _horas(consulta) == ['01 09:30', '01 13:00', '02 10:00', '03 08:00']


# ── Por HTTP, desde el panel ──────────────────────────────────────────────────

def test_la_pantalla_filtra_por_franja(auth_client, db, atenciones):
    res = auth_client.get('/registros-atencion?start_date=2026-09-01'
                          '&end_date=2026-09-03&start_time=22:00&end_time=06:00')

    assert res.status_code == 200
    texto = res.get_data(as_text=True)
    assert '23:00' in texto and '22:30' in texto
    assert '16:00' not in texto


def test_las_horas_vuelven_al_formulario(auth_client, db, atenciones):
    """Si no vuelven, el filtro se borra visualmente en cada busqueda."""
    res = auth_client.get('/registros-atencion?start_time=22:00&end_time=06:00')

    texto = res.get_data(as_text=True)
    assert 'value="22:00"' in texto
    assert 'value="06:00"' in texto


def _textos_del_xlsx(datos: bytes) -> str:
    """El texto de un xlsx, sin depender de openpyxl.

    Un xlsx es un zip de XML y xlsxwriter mete las cadenas en sharedStrings,
    asi que para comprobar que horas salen basta con leerlo de ahi. Se hace asi
    para no meter una dependencia nueva solo para esta prueba.
    """
    import zipfile
    from io import BytesIO

    with zipfile.ZipFile(BytesIO(datos)) as z:
        return z.read('xl/sharedStrings.xml').decode('utf-8')


def test_el_excel_filtra_lo_mismo_que_la_pantalla(auth_client, db, atenciones):
    """El bloque de filtros estuvo duplicado: si se separan, el Excel miente."""
    res = auth_client.get('/exportar_atenciones_excel?start_date=2026-09-01'
                          '&end_date=2026-09-03&start_time=22:00&end_time=06:00')

    assert res.status_code == 200
    texto = _textos_del_xlsx(res.data)
    for hora in ('03:00', '05:00', '22:30', '23:00'):
        assert hora in texto, f'falta {hora} en el Excel'
    for hora in ('09:30', '13:00', '10:00', '08:00', '16:00'):
        assert hora not in texto, f'{hora} no deberia estar en el Excel'


def test_sin_sesion_no_se_consultan_las_atenciones(client, db):
    res = client.get('/registros-atencion?start_time=22:00')
    assert res.status_code in (301, 302)
    assert '/admin/login' in res.headers['Location']
