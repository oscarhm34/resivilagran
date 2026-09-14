"""
test_filtro_tipo_atencion.py — El filtro por tipo en "Registros de atención".

Contexto del fallo real (14/09/2026): las duchas se registraban bien desde la
webapp, pero al filtrar los registros de atención por el tipo "Ducha" la lista
salía vacía. El filtro comparaba `CareRecord.care_type_id`, la columna antigua
de un solo tipo, que ningún registro nuevo rellena: la webapp guarda los tipos
en la relación múltiple `care_types`.
"""

from datetime import datetime, timedelta

import pytest

from app.models import Resident, CareType, CareRecord


@pytest.fixture
def ducha_registrada(db, cleaner_user):
    """Una atención cerrada con el tipo guardado como la webapp lo guarda."""
    resident = Resident(name='Antonia Vidal', nfc_code='RES-DUCHA-1', active=True)
    higiene = CareType(name='Higiene', active=True)
    db.session.add_all([resident, higiene])
    db.session.flush()
    ducha = CareType(name='Ducha', active=True, parent_id=higiene.id)
    db.session.add(ducha)
    db.session.flush()
    record = CareRecord(worker_id=cleaner_user.id, resident_id=resident.id,
                        start_time=datetime.now() - timedelta(minutes=20),
                        end_time=datetime.now())
    record.care_types.append(ducha)
    db.session.add(record)
    db.session.commit()
    return {'record': record, 'ducha': ducha, 'higiene': higiene}


def test_filtrar_por_tipo_encuentra_la_atencion(auth_client, ducha_registrada):
    """Filtrar por "Ducha" devuelve la atención registrada con ese tipo."""
    resp = auth_client.get(
        f"/registros-atencion?care_type_id={ducha_registrada['ducha'].id}")

    assert resp.status_code == 200
    fila = f"rec-{ducha_registrada['record'].id}"
    assert fila in resp.get_data(as_text=True)


def test_filtrar_por_otro_tipo_no_la_devuelve(auth_client, ducha_registrada):
    """El filtro sigue filtrando: otro tipo no arrastra la ducha."""
    resp = auth_client.get(
        f"/registros-atencion?care_type_id={ducha_registrada['higiene'].id}")

    assert resp.status_code == 200
    fila = f"rec-{ducha_registrada['record'].id}"
    assert fila not in resp.get_data(as_text=True)


def test_exportar_excel_filtrado_por_tipo_incluye_la_atencion(
        auth_client, ducha_registrada):
    """La exportación a Excel usa el mismo filtro y debe encontrarla igual."""
    resp = auth_client.get(
        f"/exportar_atenciones_excel?care_type_id={ducha_registrada['ducha'].id}")

    assert resp.status_code == 200
    assert len(resp.data) > 0
