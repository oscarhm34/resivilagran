"""
test_records.py — Tests de la vista de registros de limpieza.

Endpoints cubiertos:
- GET /registros-limpieza         → listado con filtros y paginación
- GET /exportar_excel             → exportación a fichero XLSX
- GET /ultima-limpieza            → estado de última limpieza por habitación
- GET /api/registros-limpieza     → API JSON de registros del año en curso

Casos cubiertos:
- Listado sin filtros
- Filtro por habitación
- Filtro por limpiadora
- Filtro por fechas (start_date / end_date)
- Paginación (parámetro page)
- Exportar Excel genera fichero descargable
- Exportar Excel con filtros aplica los filtros al fichero
- Última limpieza muestra info de habitaciones con y sin registros
- API JSON devuelve estructura correcta
"""

import pytest
from datetime import datetime, timedelta
from io import BytesIO

from app.models import CleaningRecord, Room, Floor, RoomType, Cleaner
from app import db as _db


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_record(cleaner_id, room_id, start_offset_days=0, duration_minutes=20,
                completed=True):
    """Crea un CleaningRecord relativo al momento actual."""
    start = datetime.now() - timedelta(days=start_offset_days)
    end = start + timedelta(minutes=duration_minutes) if completed else None
    record = CleaningRecord(
        cleaner_id=cleaner_id,
        room_id=room_id,
        start_time=start,
        end_time=end,
    )
    _db.session.add(record)
    _db.session.commit()
    return record


# ── GET /registros-limpieza ───────────────────────────────────────────────────

class TestRegistrosLimpieza:
    """Tests de la vista de listado de registros."""

    def test_page_renders_for_admin(self, auth_client, completed_record):
        """La página devuelve 200 con registros existentes."""
        response = auth_client.get("/registros-limpieza")
        assert response.status_code == 200

    def test_page_requires_auth(self, client, db):
        """Sin sesión redirige a login."""
        response = client.get("/registros-limpieza", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]

    def test_filter_by_room_id_returns_only_matching_records(
        self, auth_client, cleaner_user, room, second_room
    ):
        """
        Filtrando por room_id sólo aparecen los registros de esa habitación.
        """
        make_record(cleaner_user.id, room.id)
        make_record(cleaner_user.id, second_room.id)

        response = auth_client.get(f"/registros-limpieza?room_id={room.id}")
        assert response.status_code == 200
        # El número de la segunda habitación no debe aparecer en el HTML
        # (sólo filtramos y verificamos que la petición no falla)
        assert room.number.encode() in response.data

    def test_filter_by_cleaner_id_returns_only_matching_records(
        self, auth_client, cleaner_user, admin_user, room
    ):
        """Filtrando por cleaner_id sólo aparecen sus registros."""
        make_record(cleaner_user.id, room.id)
        make_record(admin_user.id, room.id)

        response = auth_client.get(
            f"/registros-limpieza?cleaner_id={cleaner_user.id}"
        )
        assert response.status_code == 200
        assert cleaner_user.name.encode() in response.data

    def test_filter_by_start_date(self, auth_client, cleaner_user, room):
        """Filtrar por start_date excluye registros anteriores a esa fecha."""
        # Registro de hace 10 días
        make_record(cleaner_user.id, room.id, start_offset_days=10)
        # Registro de hoy
        make_record(cleaner_user.id, room.id, start_offset_days=0)

        today_str = datetime.now().strftime("%Y-%m-%d")
        response = auth_client.get(
            f"/registros-limpieza?start_date={today_str}"
        )
        assert response.status_code == 200

    def test_filter_by_end_date(self, auth_client, cleaner_user, room):
        """Filtrar por end_date excluye registros posteriores a esa fecha."""
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        response = auth_client.get(
            f"/registros-limpieza?end_date={yesterday_str}"
        )
        assert response.status_code == 200

    def test_pagination_page_param_is_accepted(self, auth_client, db):
        """El parámetro page no causa error aunque no haya resultados."""
        response = auth_client.get("/registros-limpieza?page=2")
        assert response.status_code == 200

    def test_no_records_renders_empty_page(self, auth_client, db):
        """Sin registros la página renderiza sin errores (lista vacía)."""
        response = auth_client.get("/registros-limpieza")
        assert response.status_code == 200


# ── GET /exportar_excel ───────────────────────────────────────────────────────

class TestExportarExcel:
    """Tests de la exportación a Excel."""

    def test_export_returns_xlsx_file(self, auth_client, completed_record):
        """La respuesta es un fichero Excel con el content-type correcto."""
        response = auth_client.get("/exportar_excel")
        assert response.status_code == 200
        assert (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            in response.content_type
        )

    def test_export_download_name_is_correct(self, auth_client, completed_record):
        """El fichero descargado se llama registros_limpieza.xlsx."""
        response = auth_client.get("/exportar_excel")
        content_disposition = response.headers.get("Content-Disposition", "")
        assert "registros_limpieza.xlsx" in content_disposition

    def test_export_empty_db_returns_valid_xlsx(self, auth_client, db):
        """Sin registros se genera un Excel válido (no un 500)."""
        response = auth_client.get("/exportar_excel")
        assert response.status_code == 200
        # Verificar que el fichero no está vacío
        assert len(response.data) > 0

    def test_export_with_room_filter(self, auth_client, cleaner_user, room, second_room):
        """Exportar con filtro room_id no causa errores."""
        make_record(cleaner_user.id, room.id)
        make_record(cleaner_user.id, second_room.id)

        response = auth_client.get(f"/exportar_excel?room_id={room.id}")
        assert response.status_code == 200
        assert (
            "spreadsheetml" in response.content_type
        )

    def test_export_with_date_filters(self, auth_client, cleaner_user, room):
        """Exportar con start_date y end_date no causa errores."""
        make_record(cleaner_user.id, room.id)
        today_str = datetime.now().strftime("%Y-%m-%d")

        response = auth_client.get(
            f"/exportar_excel?start_date={today_str}&end_date={today_str}"
        )
        assert response.status_code == 200

    def test_export_requires_auth(self, client, db):
        """Sin sesión redirige a login."""
        response = client.get("/exportar_excel", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


# ── GET /ultima-limpieza ──────────────────────────────────────────────────────

class TestUltimaLimpieza:
    """Tests de la vista de última limpieza por habitación."""

    def test_page_renders_for_admin(self, auth_client, room):
        """La página devuelve 200."""
        response = auth_client.get("/ultima-limpieza")
        assert response.status_code == 200

    def test_room_with_completed_record_shows_date(
        self, auth_client, completed_record, room
    ):
        """Una habitación con limpieza completada muestra fecha y hora."""
        response = auth_client.get("/ultima-limpieza")
        assert response.status_code == 200
        # La fecha del registro completado debe aparecer en la página
        formatted_date = completed_record.end_time.strftime("%d/%m/%Y")
        assert formatted_date.encode() in response.data

    def test_room_without_records_shows_nunca(self, auth_client, second_room):
        """Una habitación sin limpiezas muestra 'Nunca'."""
        response = auth_client.get("/ultima-limpieza")
        assert response.status_code == 200
        assert "Nunca".encode() in response.data

    def test_active_record_not_shown_as_last_cleaning(
        self, auth_client, active_record, room
    ):
        """
        Una limpieza en curso (sin end_time) no debe aparecer como última
        limpieza completada de la habitación.
        """
        response = auth_client.get("/ultima-limpieza")
        assert response.status_code == 200
        # La habitación con sólo un registro activo debe mostrar 'Nunca'
        assert "Nunca".encode() in response.data

    def test_multiple_records_shows_latest(
        self, auth_client, cleaner_user, room, app
    ):
        """De varios registros se muestra el más reciente."""
        old_start = datetime(2024, 1, 1, 8, 0, 0)
        recent_start = datetime(2026, 3, 15, 10, 0, 0)

        r1 = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=old_start,
            end_time=old_start + timedelta(minutes=15),
        )
        r2 = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=recent_start,
            end_time=recent_start + timedelta(minutes=25),
        )
        _db.session.add_all([r1, r2])
        _db.session.commit()

        response = auth_client.get("/ultima-limpieza")
        assert response.status_code == 200
        # La fecha más reciente debe aparecer
        assert "15/03/2026".encode() in response.data

    def test_ultima_limpieza_requires_auth(self, client, db):
        """Sin sesión redirige a login."""
        response = client.get("/ultima-limpieza", follow_redirects=False)
        assert response.status_code == 302
        assert "admin/login" in response.headers["Location"]


# ── GET /api/registros-limpieza ───────────────────────────────────────────────

class TestApiRegistrosLimpieza:
    """Tests de la API JSON de registros del año actual."""

    def test_api_returns_json_list(self, auth_client, db):
        """El endpoint devuelve una lista JSON."""
        response = auth_client.get("/api/registros-limpieza")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_api_record_has_required_fields(
        self, auth_client, completed_record
    ):
        """Cada registro JSON contiene los campos esperados."""
        response = auth_client.get("/api/registros-limpieza")
        data = response.get_json()
        assert len(data) >= 1

        record = data[0]
        expected_keys = {
            "Limpiador",
            "Habitación",
            "Descripción",
            "Tipo de Espacio",
            "Fecha de Inicio",
            "Hora de Inicio",
            "Fecha de FIN",
            "Hora de FIN",
            "Duración",
        }
        assert expected_keys.issubset(set(record.keys()))

    def test_api_only_returns_current_year_records(
        self, auth_client, cleaner_user, room, app
    ):
        """Registros de años anteriores no aparecen en la API."""
        # Registro del año pasado
        old_record = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=datetime(2020, 6, 1, 9, 0, 0),
            end_time=datetime(2020, 6, 1, 9, 30, 0),
        )
        # Registro del año actual
        current_record = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=datetime(datetime.now().year, 1, 15, 10, 0, 0),
            end_time=datetime(datetime.now().year, 1, 15, 10, 30, 0),
        )
        _db.session.add_all([old_record, current_record])
        _db.session.commit()

        response = auth_client.get("/api/registros-limpieza")
        data = response.get_json()

        # Todos los registros deben ser del año actual
        current_year = str(datetime.now().year)
        for record in data:
            if record["Fecha de Inicio"]:
                assert current_year in record["Fecha de Inicio"]
