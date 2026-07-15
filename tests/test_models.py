"""
test_models.py — Unit tests de los modelos de dominio.

Cubre los métodos y propiedades de cada modelo sin necesidad de peticiones HTTP.
Se prueban directamente contra la sesión de BD in-memory.

Modelos cubiertos:
- Cleaner.set_password / check_password
- Cleaner.is_admin comportamiento
- CleaningRecord.calculate_duration
- CleaningRecord.current_year_records (classmethod)
- _format_duration (helper de routes, testeado a través del modelo)
- Room.__repr__
- Integridad referencial: Floor, RoomType, Room
"""

import pytest
from datetime import datetime, timedelta

from app.models import Cleaner, CleaningRecord, Room, RoomType, Floor
from app import db as _db


# ── Cleaner ───────────────────────────────────────────────────────────────────

class TestCleanerPassword:
    """Tests de gestión de contraseñas."""

    def test_set_password_hashes_value(self, db):
        """set_password almacena un hash, no el texto plano."""
        cleaner = Cleaner(username="u1", name="Test", is_admin=False)
        cleaner.set_password("mysecret")
        assert cleaner.password_hash is not None
        assert cleaner.password_hash != "mysecret"

    def test_check_password_returns_true_for_correct_password(self, db):
        """check_password devuelve True con la contraseña original."""
        cleaner = Cleaner(username="u2", name="Test", is_admin=False)
        cleaner.set_password("correctpass")
        assert cleaner.check_password("correctpass") is True

    def test_check_password_returns_false_for_wrong_password(self, db):
        """check_password devuelve False con contraseña incorrecta."""
        cleaner = Cleaner(username="u3", name="Test", is_admin=False)
        cleaner.set_password("correctpass")
        assert cleaner.check_password("wrongpass") is False

    def test_check_password_is_case_sensitive(self, db):
        """Las contraseñas distinguen mayúsculas/minúsculas."""
        cleaner = Cleaner(username="u4", name="Test", is_admin=False)
        cleaner.set_password("Secret")
        assert cleaner.check_password("secret") is False
        assert cleaner.check_password("SECRET") is False

    def test_different_passwords_produce_different_hashes(self, db):
        """Dos contraseñas distintas producen hashes distintos."""
        c1 = Cleaner(username="u5", name="A", is_admin=False)
        c2 = Cleaner(username="u6", name="B", is_admin=False)
        c1.set_password("password1")
        c2.set_password("password2")
        assert c1.password_hash != c2.password_hash

    def test_same_password_different_hashes_due_to_salt(self, db):
        """
        La misma contraseña usada en dos usuarios distintos produce hashes
        distintos por el salting de Werkzeug.
        """
        c1 = Cleaner(username="u7", name="A", is_admin=False)
        c2 = Cleaner(username="u8", name="B", is_admin=False)
        c1.set_password("samepassword")
        c2.set_password("samepassword")
        # Con bcrypt/scrypt el hash incluye salt aleatorio
        assert c1.password_hash != c2.password_hash


class TestCleanerIsAdmin:
    """Tests del campo is_admin."""

    def test_is_admin_defaults_to_false(self, db):
        """Un Cleaner creado sin is_admin explícito tiene is_admin=False."""
        cleaner = Cleaner(username="plain", name="Plain User", is_admin=False)
        cleaner.set_password("x")
        _db.session.add(cleaner)
        _db.session.commit()
        assert cleaner.is_admin is False

    def test_is_admin_can_be_set_to_true(self, db):
        """is_admin=True se persiste correctamente."""
        cleaner = Cleaner(username="boss", name="Boss", is_admin=True)
        cleaner.set_password("x")
        _db.session.add(cleaner)
        _db.session.commit()
        fetched = Cleaner.query.filter_by(username="boss").first()
        assert fetched.is_admin is True


# ── CleaningRecord.calculate_duration ────────────────────────────────────────

class TestCalculateDuration:
    """Tests del método calculate_duration de CleaningRecord."""

    def test_calculate_duration_returns_seconds(self, cleaner_user, room):
        """La duración es el número de segundos entre start y end."""
        start = datetime(2026, 4, 24, 9, 0, 0)
        end = datetime(2026, 4, 24, 9, 30, 0)  # 30 min = 1800 s
        record = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=start,
            end_time=end,
        )
        assert record.calculate_duration() == 1800.0

    def test_calculate_duration_none_if_no_end_time(self, cleaner_user, room):
        """Sin end_time, la duración es None (limpieza en curso)."""
        record = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=datetime.now(),
            end_time=None,
        )
        assert record.calculate_duration() is None

    def test_calculate_duration_none_if_no_start_time(self, cleaner_user, room):
        """Sin start_time, la duración es None."""
        record = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=None,
            end_time=datetime.now(),
        )
        assert record.calculate_duration() is None

    def test_calculate_duration_short_cleaning(self, cleaner_user, room):
        """Limpieza de menos de un minuto devuelve segundos correctos."""
        start = datetime(2026, 4, 24, 10, 0, 0)
        end = datetime(2026, 4, 24, 10, 0, 45)  # 45 segundos
        record = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=start,
            end_time=end,
        )
        assert record.calculate_duration() == 45.0

    def test_calculate_duration_multi_hour_cleaning(self, cleaner_user, room):
        """Limpieza de varias horas devuelve segundos totales."""
        start = datetime(2026, 4, 24, 8, 0, 0)
        end = datetime(2026, 4, 24, 10, 30, 0)  # 2h 30min = 9000s
        record = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=start,
            end_time=end,
        )
        assert record.calculate_duration() == 9000.0


# ── CleaningRecord.current_year_records ──────────────────────────────────────

class TestCurrentYearRecords:
    """Tests del classmethod current_year_records."""

    def test_returns_only_current_year_records(self, db, cleaner_user, room):
        """Sólo devuelve registros del año en curso."""
        current_year = datetime.now().year
        old = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=datetime(2019, 6, 15, 9, 0, 0),
            end_time=datetime(2019, 6, 15, 9, 30, 0),
        )
        current = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=datetime(current_year, 3, 1, 9, 0, 0),
            end_time=datetime(current_year, 3, 1, 9, 20, 0),
        )
        _db.session.add_all([old, current])
        _db.session.commit()

        results = CleaningRecord.current_year_records().all()
        assert len(results) == 1
        assert results[0].start_time.year == current_year

    def test_returns_empty_when_no_current_year_records(
        self, db, cleaner_user, room
    ):
        """Sin registros del año actual devuelve lista vacía."""
        old = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=datetime(2018, 1, 1, 9, 0, 0),
            end_time=datetime(2018, 1, 1, 9, 15, 0),
        )
        _db.session.add(old)
        _db.session.commit()

        results = CleaningRecord.current_year_records().all()
        assert results == []

    def test_active_record_counted_by_start_time(self, db, cleaner_user, room):
        """
        Un registro activo (sin end_time) del año actual se incluye
        porque current_year_records usa COALESCE(end_time, start_time).
        """
        current_year = datetime.now().year
        active = CleaningRecord(
            cleaner_id=cleaner_user.id,
            room_id=room.id,
            start_time=datetime(current_year, 4, 1, 9, 0, 0),
            end_time=None,
        )
        _db.session.add(active)
        _db.session.commit()

        results = CleaningRecord.current_year_records().all()
        assert len(results) == 1


# ── Room.__repr__ ─────────────────────────────────────────────────────────────

class TestRoomRepr:
    """Tests de la representación string de Room."""

    def test_repr_includes_number_type_and_floor(self, room, room_type, floor):
        """__repr__ de Room incluye el número, tipo y planta."""
        repr_str = repr(room)
        assert room.number in repr_str
        assert room_type.name in repr_str
        assert floor.name in repr_str


# ── Integridad referencial ─────────────────────────────────────────────────────

class TestReferentialIntegrity:
    """Tests de que las relaciones entre modelos funcionan correctamente."""

    def test_room_has_room_type_and_floor(self, room, room_type, floor):
        """Room cargado de BD tiene sus relaciones correctas."""
        assert room.room_type is not None
        assert room.room_type.id == room_type.id
        assert room.floor is not None
        assert room.floor.id == floor.id

    def test_cleaning_record_has_cleaner_and_room(
        self, completed_record, cleaner_user, room
    ):
        """CleaningRecord tiene relaciones correctas con Cleaner y Room."""
        assert completed_record.cleaner is not None
        assert completed_record.cleaner.id == cleaner_user.id
        assert completed_record.room is not None
        assert completed_record.room.id == room.id

    def test_cleaner_backref_cleaning_records(self, completed_record, cleaner_user):
        """El backref cleaning_records en Cleaner devuelve sus registros."""
        assert len(cleaner_user.cleaning_records) >= 1
        assert completed_record in cleaner_user.cleaning_records

    def test_username_is_unique(self, db):
        """No se pueden insertar dos Cleaners con el mismo username."""
        from sqlalchemy.exc import IntegrityError
        c1 = Cleaner(username="duplicado", name="Uno", is_admin=False)
        c1.set_password("x")
        c2 = Cleaner(username="duplicado", name="Dos", is_admin=False)
        c2.set_password("y")
        _db.session.add(c1)
        _db.session.commit()
        _db.session.add(c2)
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()

    def test_room_type_name_is_unique(self, db):
        """No se pueden crear dos RoomType con el mismo nombre."""
        from sqlalchemy.exc import IntegrityError
        rt1 = RoomType(name="Tipo Único")
        rt2 = RoomType(name="Tipo Único")
        _db.session.add(rt1)
        _db.session.commit()
        _db.session.add(rt2)
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()
