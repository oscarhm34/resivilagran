"""
test_control_diario.py — Informe de control diario (residents.control_diario).

Sustituye el Excel que se montaba a mano cada madrugada. Como sirve para
justificar horas, lo que mas importa aqui es que los totales sean defendibles:
que un registro sin cerrar no invente horas y que una sesion olvidada de doce
horas no se cuele en la suma sin que nadie lo vea.
"""

import pytest
from datetime import datetime, timedelta, time as dt_time

from app import db as _db
from app.models import Cleaner, Resident, CareType, CareRecord, CleaningRecord
from app.blueprints.residents import _construir_control, _duracion_larga


HOY = datetime.now().date()


def _momento(hora, minuto=0, dia=None):
    return datetime.combine(dia or HOY, dt_time(hora, minuto))


def _construir(fecha=None, noche_inicio=dt_time(22, 0), noche_fin=dt_time(7, 0),
               umbral_segundos=7200):
    return _construir_control(fecha or HOY, noche_inicio, noche_fin, umbral_segundos)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def residente(db):
    r = Resident(name="Residente Uno", nfc_code="RES-001", active=True)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def tipo_wc(db):
    t = CareType(name="Acompanar al WC", control_diario=True,
                 minimo_diario=2, control_etiqueta="WC")
    db.session.add(t)
    db.session.commit()
    return t


def _atencion(worker, residente, inicio, fin=None, tipos=(), tipo_legacy=None):
    registro = CareRecord(worker_id=worker.id, resident_id=residente.id,
                          start_time=inicio, end_time=fin)
    if tipo_legacy is not None:
        registro.care_type_id = tipo_legacy.id
    _db.session.add(registro)
    _db.session.flush()
    for tipo in tipos:
        registro.care_types.append(tipo)
    _db.session.commit()
    return registro


# ── Autorizacion ─────────────────────────────────────────────────────────────

def test_sin_sesion_no_entra(client):
    resp = client.get("/control-diario")
    assert resp.status_code != 200


def test_sin_admin_no_entra(client, cleaner_user):
    client.post("/admin/login",
                data={"username": "limpiadora1", "password": "limpia123"},
                follow_redirects=True)
    resp = client.get("/control-diario")
    assert resp.status_code != 200


def test_admin_entra(auth_client):
    assert auth_client.get("/control-diario").status_code == 200


# ── Entrada invalida ─────────────────────────────────────────────────────────

def test_fecha_invalida_no_revienta(auth_client):
    assert auth_client.get("/control-diario?fecha=ayer").status_code == 200


def test_horas_y_umbral_invalidos_no_revientan(auth_client):
    resp = auth_client.get("/control-diario"
                           "?noche_inicio=99:99&noche_fin=&umbral_horas=abc")
    assert resp.status_code == 200


# ── Camino correcto ──────────────────────────────────────────────────────────

def test_suma_atenciones_y_limpiezas_de_la_misma_trabajadora(
        db, cleaner_user, residente, room):
    _atencion(cleaner_user, residente, _momento(9), _momento(9, 30))
    db.session.add(CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                                  start_time=_momento(10),
                                  end_time=_momento(10, 15)))
    db.session.commit()

    datos = _construir()
    grupos = datos["por_trabajadora"]
    assert len(grupos) == 1
    # 30 min de atencion + 15 de limpieza: el total del dia de esa persona.
    assert grupos[0]["total"] == "00:45:00"
    assert {f.origen for f in grupos[0]["filas"]} == {"atencion", "limpieza"}


def test_duracion_larga_pasa_de_veinticuatro_horas():
    assert _duracion_larga(90061) == "25:01:01"


# ── Lo que no se puede defender ──────────────────────────────────────────────

def test_registro_abierto_no_suma_pero_se_ensena(db, cleaner_user, residente):
    _atencion(cleaner_user, residente, _momento(9))  # sin end_time

    grupo = _construir()["por_trabajadora"][0]
    assert grupo["total"] == "00:00:00"
    assert grupo["abiertas"] == 1
    assert grupo["filas"][0].abierta is True
    assert len(grupo["filas"]) == 1, "se ensena, no se esconde"


def test_registro_anomalo_queda_fuera_del_total_y_contado(
        db, cleaner_user, room):
    # El caso real del Excel: una limpieza de 12:33:35 que nadie cerro.
    db.session.add(CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                                  start_time=_momento(8),
                                  end_time=_momento(20, 33)))
    db.session.commit()

    grupo = _construir()["por_trabajadora"][0]
    assert grupo["total"] == "00:00:00"
    assert grupo["anomalas"] == 1
    assert grupo["total_anomalo"] == "12:33:00"


def test_el_umbral_se_puede_subir(db, cleaner_user, room):
    db.session.add(CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                                  start_time=_momento(8), end_time=_momento(11)))
    db.session.commit()

    assert _construir()["por_trabajadora"][0]["anomalas"] == 1
    sin_anomalas = _construir(umbral_segundos=4 * 3600)["por_trabajadora"][0]
    assert sin_anomalas["anomalas"] == 0
    assert sin_anomalas["total"] == "03:00:00"


# ── La noche ─────────────────────────────────────────────────────────────────

def test_la_noche_recoge_la_madrugada_siguiente(db, cleaner_user, residente):
    _atencion(cleaner_user, residente, _momento(23), _momento(23, 10))
    manana = HOY + timedelta(days=1)
    _atencion(cleaner_user, residente,
              _momento(2, 0, dia=manana), _momento(2, 10, dia=manana))
    db.session.commit()

    datos = _construir()
    assert len(datos["noche"]) == 1
    assert len(datos["noche"][0]["filas"]) == 2, "las dos son la misma noche"
    # Y ninguna de las dos cuenta como trabajo de dia.
    assert datos["por_trabajadora"] == []


def test_lo_de_antes_de_la_noche_va_al_bloque_de_dia(db, cleaner_user, residente):
    _atencion(cleaner_user, residente, _momento(10), _momento(10, 20))
    db.session.commit()

    datos = _construir()
    assert len(datos["por_trabajadora"]) == 1
    assert datos["noche"] == []


def test_por_residente_cubre_el_dia_natural_entero(db, cleaner_user, residente):
    _atencion(cleaner_user, residente, _momento(10), _momento(10, 20))
    _atencion(cleaner_user, residente, _momento(23), _momento(23, 10))
    db.session.commit()

    grupos = _construir()["por_residente"]
    assert len(grupos) == 1
    assert len(grupos[0]["filas"]) == 2, "dia y noche, como la hoja de siempre"
    assert grupos[0]["total"] == "00:30:00"


# ── Marcas de control ────────────────────────────────────────────────────────

def test_residente_sin_ningun_registro_sale_con_sus_marcas(
        db, residente, tipo_wc):
    datos = _construir()
    assert datos["por_residente"] == []
    sin_nada = datos["sin_registros"]
    assert len(sin_nada) == 1
    assert sin_nada[0]["marcas"][0]["texto"] == "NO WC"


def test_marca_por_debajo_del_minimo(db, cleaner_user, residente, tipo_wc):
    _atencion(cleaner_user, residente, _momento(9), _momento(9, 5), tipos=[tipo_wc])
    db.session.commit()

    marcas = _construir()["por_residente"][0]["marcas"]
    assert [m["texto"] for m in marcas] == ["1 WC"], "se esperaban 2"


def test_sin_marca_al_llegar_al_minimo(db, cleaner_user, residente, tipo_wc):
    _atencion(cleaner_user, residente, _momento(9), _momento(9, 5), tipos=[tipo_wc])
    _atencion(cleaner_user, residente, _momento(17), _momento(17, 5), tipos=[tipo_wc])
    db.session.commit()

    assert _construir()["por_residente"][0]["marcas"] == []


def test_tipo_legacy_cuenta_igual_que_la_relacion_multiple(
        db, cleaner_user, residente, tipo_wc):
    """La regresion de siempre: el tipo vive en dos sitios.

    Los registros anteriores a los tipos multiples solo tienen la columna
    antigua. Mirar solo la relacion nueva los dejaria fuera del recuento y el
    residente saldria con "NO WC" teniendolo hecho.
    """
    _atencion(cleaner_user, residente, _momento(9), _momento(9, 5),
              tipo_legacy=tipo_wc)
    _atencion(cleaner_user, residente, _momento(17), _momento(17, 5),
              tipos=[tipo_wc])
    db.session.commit()

    assert _construir()["por_residente"][0]["marcas"] == []


def test_un_tipo_padre_cuenta_a_sus_hijos(db, cleaner_user, residente):
    padre = CareType(name="Higiene", control_diario=True, control_etiqueta="HIGIENE")
    db.session.add(padre)
    db.session.flush()
    hijo = CareType(name="Limpieza dientes", parent_id=padre.id)
    db.session.add(hijo)
    db.session.commit()

    _atencion(cleaner_user, residente, _momento(9), _momento(9, 5), tipos=[hijo])
    db.session.commit()

    # En el registro solo se guarda el hijo: sin expandir, saldria "NO HIGIENE".
    assert _construir()["por_residente"][0]["marcas"] == []


def test_sin_historial_en_dos_semanas_la_marca_no_grita(db, residente, tipo_wc):
    """Una persona encamada no se levanta nunca: eso no es un descuido."""
    marca = _construir()["sin_registros"][0]["marcas"][0]
    assert marca["sin_historial"] is True


def test_con_historial_reciente_la_marca_si_avisa(
        db, cleaner_user, residente, tipo_wc):
    ayer = HOY - timedelta(days=1)
    _atencion(cleaner_user, residente,
              _momento(9, 0, dia=ayer), _momento(9, 5, dia=ayer), tipos=[tipo_wc])
    db.session.commit()

    marca = _construir()["sin_registros"][0]["marcas"][0]
    assert marca["texto"] == "NO WC"
    assert marca["sin_historial"] is False, "ayer si la recibio: hoy es un aviso"


def test_recuento_por_trabajadora(db, cleaner_user, residente, tipo_wc):
    otro = Resident(name="Residente Dos", nfc_code="RES-002", active=True)
    db.session.add(otro)
    db.session.commit()
    _atencion(cleaner_user, residente, _momento(9), _momento(9, 5), tipos=[tipo_wc])
    _atencion(cleaner_user, otro, _momento(10), _momento(10, 5), tipos=[tipo_wc])
    db.session.commit()

    assert _construir()["por_trabajadora"][0]["recuentos"] == ["WC 2"]


# ── Bordes ───────────────────────────────────────────────────────────────────

def test_limpieza_sin_hora_de_inicio_se_ignora(db, cleaner_user, room):
    # CleaningRecord.start_time es nullable, al contrario que el de atenciones.
    db.session.add(CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                                  start_time=None, end_time=_momento(10)))
    db.session.commit()

    assert _construir()["por_trabajadora"] == []


def test_residente_inactivo_no_sale_como_olvidado(db, residente, tipo_wc):
    residente.active = False
    db.session.commit()

    assert _construir()["sin_registros"] == []


def test_el_recuento_de_la_noche_sale_de_la_noche(db, cleaner_user, residente, tipo_wc):
    """A la de noche no se le puede atribuir lo que hizo la del turno anterior."""
    _atencion(cleaner_user, residente, _momento(10), _momento(10, 5), tipos=[tipo_wc])
    _atencion(cleaner_user, residente, _momento(23), _momento(23, 5), tipos=[tipo_wc])
    db.session.commit()

    datos = _construir()
    assert datos["por_trabajadora"][0]["recuentos"] == ["WC 1"]
    assert datos["noche"][0]["recuentos"] == ["WC 1"]


def test_la_pagina_se_pinta_con_datos(auth_client, db, cleaner_user, residente,
                                      tipo_wc, room):
    """Rendering con filas de verdad: la pagina vacia no pasa por las tablas."""
    _atencion(cleaner_user, residente, _momento(9), _momento(9, 5), tipos=[tipo_wc])
    _atencion(cleaner_user, residente, _momento(23), _momento(23, 5))
    _atencion(cleaner_user, residente, _momento(11))  # sin cerrar
    db.session.add(CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                                  start_time=_momento(8), end_time=_momento(20)))
    db.session.commit()

    resp = auth_client.get(f"/control-diario?fecha={HOY.isoformat()}")
    assert resp.status_code == 200
    cuerpo = resp.get_data(as_text=True)
    assert "SIN CERRAR" in cuerpo
    assert "REVISAR" in cuerpo
    assert "1 WC" in cuerpo
    assert "Residente Uno" in cuerpo


def test_el_historial_tambien_ve_el_tipo_antiguo(db, cleaner_user, residente, tipo_wc):
    """La consulta del historial filtra por tipo en SQL: los dos sitios cuentan."""
    ayer = HOY - timedelta(days=1)
    _atencion(cleaner_user, residente, _momento(9, 0, dia=ayer),
              _momento(9, 5, dia=ayer), tipo_legacy=tipo_wc)
    db.session.commit()

    marca = _construir()["sin_registros"][0]["marcas"][0]
    assert marca["sin_historial"] is False


def test_una_franja_que_no_cruza_medianoche_no_pierde_registros(
        db, cleaner_user, residente):
    """El bloque de dia es el dia menos la noche, no "lo anterior a la noche".

    Con 02:00-06:00, mirar solo lo anterior dejaria lo de las 10:00 fuera de
    los dos bloques: desaparecido de un informe que sirve para justificar horas.
    """
    _atencion(cleaner_user, residente, _momento(3), _momento(3, 10))
    _atencion(cleaner_user, residente, _momento(10), _momento(10, 20))
    db.session.commit()

    datos = _construir(noche_inicio=dt_time(2, 0), noche_fin=dt_time(6, 0))
    assert datos["noche"][0]["total"] == "00:10:00"
    assert datos["por_trabajadora"][0]["total"] == "00:20:00"
    # Y las dos filas siguen estando, una en cada bloque.
    assert len(datos["noche"][0]["filas"]) == 1
    assert len(datos["por_trabajadora"][0]["filas"]) == 1


def test_la_madrugada_no_se_cuenta_en_dos_informes(db, cleaner_user, residente):
    """Cada registro sale en un solo bloque de un solo dia.

    La madrugada de hoy pertenece a la noche de ayer. Si ademas apareciera en
    el bloque de dia de hoy, sumar los totales de dos dias seguidos contaria
    esas horas dos veces, y el papel dejaria de justificar nada.
    """
    manana = HOY + timedelta(days=1)
    _atencion(cleaner_user, residente,
              _momento(3, 0, dia=manana), _momento(3, 30, dia=manana))
    db.session.commit()

    de_hoy = _construir(fecha=HOY)
    de_manana = _construir(fecha=manana)

    assert de_hoy["noche"][0]["total"] == "00:30:00", "es la noche de hoy"
    assert de_manana["por_trabajadora"] == [], "y no vuelve a contarse manana"


def test_la_madrugada_si_sale_en_el_bloque_por_residente(db, cleaner_user, residente):
    """El bloque por residente es el dia natural: ahi si cuadra con la hoja vieja."""
    manana = HOY + timedelta(days=1)
    _atencion(cleaner_user, residente,
              _momento(3, 0, dia=manana), _momento(3, 30, dia=manana))
    db.session.commit()

    grupos = _construir(fecha=manana)["por_residente"]
    assert len(grupos) == 1
    assert grupos[0]["total"] == "00:30:00"


# ── Que la hoja se parezca a la que se hacia a mano ──────────────────────────

def test_el_bloque_por_residente_dice_quien_le_atendio(auth_client, db,
                                                       cleaner_user, residente):
    """En la hoja de calculo la trabajadora iba en cada fila; aqui, en columna."""
    _atencion(cleaner_user, residente, _momento(9), _momento(9, 5))
    db.session.commit()

    cuerpo = auth_client.get(f"/control-diario?fecha={HOY.isoformat()}").get_data(as_text=True)
    assert 'Trabajadora' in cuerpo
    assert cleaner_user.name in cuerpo


def test_las_limpiezas_se_distinguen_de_las_atenciones(auth_client, db,
                                                       cleaner_user, residente, room):
    """Iban en un bloque aparte rotulado NETEJA; aqui van juntas y hay que verlo."""
    _atencion(cleaner_user, residente, _momento(9), _momento(9, 5))
    db.session.add(CleaningRecord(cleaner_id=cleaner_user.id, room_id=room.id,
                                  start_time=_momento(10), end_time=_momento(10, 20)))
    db.session.commit()

    datos = _construir()
    grupo = datos["por_trabajadora"][0]
    assert grupo["atenciones"] == 1
    assert grupo["limpiezas"] == 1

    cuerpo = auth_client.get(f"/control-diario?fecha={HOY.isoformat()}").get_data(as_text=True)
    assert 'limpieza' in cuerpo


def test_dice_cuanto_se_ha_ido_a_la_hoja_de_ayer(db, cleaner_user, residente):
    """Sin esto el bloque del dia parece incompleto y nadie sabe por que.

    El turno de noche que limpia a las tres de la madrugada esta en la hoja de
    ayer, porque esa es su noche; aqui solo se dice cuanto es.
    """
    _atencion(cleaner_user, residente, _momento(3), _momento(3, 20))
    _atencion(cleaner_user, residente, _momento(10), _momento(10, 10))
    db.session.commit()

    datos = _construir()
    assert datos["madrugada"] == 1
    assert datos["madrugada_total"] == "00:20:00"
    # Y no se cuenta en el bloque del dia.
    assert datos["por_trabajadora"][0]["total"] == "00:10:00"
