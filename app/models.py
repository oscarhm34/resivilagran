from __future__ import annotations
from . import db
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask_login import UserMixin
from sqlalchemy import extract, func
import hashlib


cleaner_groups = db.Table(
    'cleaner_groups',
    db.Column('cleaner_id', db.Integer, db.ForeignKey('cleaner.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('resident_group.id'), primary_key=True),
)

care_record_types = db.Table(
    'care_record_types',
    db.Column('care_record_id', db.Integer, db.ForeignKey('care_record.id'), primary_key=True),
    db.Column('care_type_id', db.Integer, db.ForeignKey('care_type.id'), primary_key=True),
)


class Cleaner(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    identity_verified = db.Column(db.Boolean, nullable=False, default=False)
    role = db.Column(db.String(20), nullable=False, default='atenciones')  # 'limpieza', 'atenciones', 'mixto', 'gestion'
    last_active = db.Column(db.DateTime, nullable=True, index=True)

    groups = db.relationship('ResidentGroup', secondary=cleaner_groups, back_populates='workers', lazy=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class CleaningRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False, index=True)
    room_id = db.Column(db.Integer, nullable=False, index=True)
    start_time = db.Column(db.DateTime, nullable=True, index=True)
    end_time = db.Column(db.DateTime, nullable=True)
    checklist_json = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    room = db.relationship('Room', primaryjoin='CleaningRecord.room_id == foreign(Room.id)', uselist=False)
    cleaner = db.relationship('Cleaner', backref=db.backref('cleaning_records', lazy=True))

    def calculate_duration(self) -> float | None:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    @classmethod
    def current_year_records(cls):
        current_year = datetime.now().year
        return cls.query.filter(
            extract('year', func.coalesce(cls.end_time, cls.start_time)) == current_year
        )


class RoomType(db.Model):
    __tablename__ = 'room_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    rooms = db.relationship('Room', back_populates='room_type')


class Floor(db.Model):
    __tablename__ = 'floor'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    rooms = db.relationship('Room', back_populates='floor')


class Room(db.Model):
    __tablename__ = 'room'
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(10), nullable=False)
    room_type_id = db.Column(db.Integer, db.ForeignKey('room_type.id'), nullable=False)
    floor_id = db.Column(db.Integer, db.ForeignKey('floor.id', name='fk_floor_id'), nullable=False)
    description = db.Column(db.Text, nullable=True)

    room_type = db.relationship('RoomType', back_populates='rooms')
    floor = db.relationship('Floor', back_populates='rooms')

    def __repr__(self) -> str:
        return f'<Room {self.number} Type {self.room_type.name} Floor {self.floor.name}>'


class ResidentGroup(db.Model):
    __tablename__ = 'resident_group'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    color = db.Column(db.String(7), nullable=False)

    residents = db.relationship('Resident', back_populates='group', lazy=True)
    workers = db.relationship('Cleaner', secondary=cleaner_groups, back_populates='groups', lazy=True)


class Resident(db.Model):
    __tablename__ = 'resident'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    nfc_code = db.Column(db.String(100), unique=True, nullable=False)
    room_number = db.Column(db.String(10), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    group_id = db.Column(db.Integer, db.ForeignKey('resident_group.id'), nullable=True)
    photo_path = db.Column(db.String(255), nullable=True)
    relevant_info = db.Column(db.Text, nullable=True)
    # Medical profile
    diagnoses = db.Column(db.Text, nullable=True)
    allergies = db.Column(db.Text, nullable=True)
    current_medication = db.Column(db.Text, nullable=True)
    blood_type = db.Column(db.String(10), nullable=True)
    dependency_level = db.Column(db.String(20), nullable=True)  # autonomous, mild, moderate, severe, total
    emergency_contact_name = db.Column(db.String(100), nullable=True)
    emergency_contact_phone = db.Column(db.String(30), nullable=True)
    emergency_contact_relation = db.Column(db.String(50), nullable=True)

    group = db.relationship('ResidentGroup', back_populates='residents')
    care_records = db.relationship('CareRecord', back_populates='resident', lazy=True)


class ResidentDocument(db.Model):
    __tablename__ = 'resident_document'
    id = db.Column(db.Integer, primary_key=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False)
    file_path = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    doc_type = db.Column(db.String(50), nullable=True)
    description = db.Column(db.Text, nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.now)

    resident = db.relationship('Resident', backref=db.backref('documents', lazy=True, cascade='all, delete-orphan'))


class CareType(db.Model):
    __tablename__ = 'care_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    icon = db.Column(db.String(10), nullable=True)
    icon_path = db.Column(db.String(255), nullable=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('care_type.id'), nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)

    children = db.relationship('CareType', backref=db.backref('parent', remote_side='CareType.id'), lazy=True)
    care_records = db.relationship('CareRecord', back_populates='care_type', lazy=True)
    vital_sign_types = db.relationship('VitalSignType', backref='care_type', lazy=True, order_by='VitalSignType.sort_order')


class VitalSignType(db.Model):
    __tablename__ = 'vital_sign_type'
    id = db.Column(db.Integer, primary_key=True)
    care_type_id = db.Column(db.Integer, db.ForeignKey('care_type.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    unit = db.Column(db.String(30), nullable=False)
    min_value = db.Column(db.Float, nullable=True)
    max_value = db.Column(db.Float, nullable=True)
    input_type = db.Column(db.String(20), default='number')
    sort_order = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)


class VitalSignReading(db.Model):
    __tablename__ = 'vital_sign_reading'
    id = db.Column(db.Integer, primary_key=True)
    care_record_id = db.Column(db.Integer, db.ForeignKey('care_record.id'), nullable=False)
    vital_sign_type_id = db.Column(db.Integer, db.ForeignKey('vital_sign_type.id'), nullable=False)
    value = db.Column(db.Float, nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    care_record = db.relationship('CareRecord', backref=db.backref('vital_sign_readings', lazy=True))
    vital_sign_type = db.relationship('VitalSignType', backref=db.backref('readings', lazy=True))


class CareRecord(db.Model):
    __tablename__ = 'care_record'
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False, index=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    care_type_id = db.Column(db.Integer, db.ForeignKey('care_type.id'), nullable=True)
    start_time = db.Column(db.DateTime, nullable=False, index=True)
    end_time = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    worker = db.relationship('Cleaner', backref=db.backref('care_records', lazy=True))
    resident = db.relationship('Resident', back_populates='care_records')
    care_type = db.relationship('CareType', back_populates='care_records')
    care_types = db.relationship('CareType', secondary=care_record_types, lazy=True)

    def calculate_duration(self) -> float | None:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None


class ChecklistItem(db.Model):
    __tablename__ = 'checklist_item'
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(200), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)


class AppSetting(db.Model):
    __tablename__ = 'app_setting'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(500), nullable=False, default='')

    @staticmethod
    def get(key: str, default: str = '') -> str:
        s = AppSetting.query.filter_by(key=key).first()
        return s.value if s else default

    @staticmethod
    def set(key: str, value: str):
        from . import db as _db
        s = AppSetting.query.filter_by(key=key).first()
        if s:
            s.value = value
        else:
            _db.session.add(AppSetting(key=key, value=value))
        _db.session.commit()


# ── IDENTITAT ─────────────────────────────────────────────────────────────────

class WorkerSelfie(db.Model):
    __tablename__ = 'worker_selfie'
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    photo_path = db.Column(db.String(255), nullable=False)
    is_reference = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    purpose = db.Column(db.String(50), nullable=True)

    cleaner = db.relationship('Cleaner', backref=db.backref('selfies', lazy=True))


# ── DOCUMENTS LEGALS ──────────────────────────────────────────────────────────

class LegalDocument(db.Model):
    __tablename__ = 'legal_document'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    doc_type = db.Column(db.String(50), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    signatures = db.relationship('DocumentSignature', back_populates='document', lazy=True)
    creator = db.relationship('Cleaner', foreign_keys=[created_by])


class DocumentSignature(db.Model):
    __tablename__ = 'document_signature'
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('legal_document.id'), nullable=False)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    signed_at = db.Column(db.DateTime, default=datetime.now)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(500), nullable=True)
    selfie_path = db.Column(db.String(255), nullable=True)
    content_hash = db.Column(db.String(64), nullable=True)

    document = db.relationship('LegalDocument', back_populates='signatures')
    cleaner = db.relationship('Cleaner', backref=db.backref('document_signatures', lazy=True))


# ── PÍNDOLES FORMATIVES ──────────────────────────────────────────────────────

class TrainingPill(db.Model):
    __tablename__ = 'training_pill'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    video_url = db.Column(db.String(500), nullable=True)
    video_duration_seconds = db.Column(db.Integer, nullable=True)
    pass_threshold = db.Column(db.Integer, default=80)
    active = db.Column(db.Boolean, default=True)
    assign_mode = db.Column(db.String(20), default='all')  # 'all' or 'selected'
    mandatory = db.Column(db.Boolean, default=False)  # blocks webapp until completed
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    questions = db.relationship('TrainingQuestion', back_populates='pill', lazy=True,
                                order_by='TrainingQuestion.sort_order')
    completions = db.relationship('TrainingCompletion', back_populates='pill', lazy=True)
    assignments = db.relationship('TrainingAssignment', back_populates='pill', lazy=True, cascade='all, delete-orphan')
    creator = db.relationship('Cleaner', foreign_keys=[created_by])


class TrainingQuestion(db.Model):
    __tablename__ = 'training_question'
    id = db.Column(db.Integer, primary_key=True)
    pill_id = db.Column(db.Integer, db.ForeignKey('training_pill.id'), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    # 'multiple' = test A/B/C/D | 'instruccion' = escuchar el audio y responder Si/No
    question_type = db.Column(db.String(20), nullable=False, default='multiple')
    option_a = db.Column(db.String(200), nullable=False)
    option_b = db.Column(db.String(200), nullable=False)
    option_c = db.Column(db.String(200), nullable=False)
    option_d = db.Column(db.String(200), nullable=False)
    correct_option = db.Column(db.String(1), nullable=False)
    sort_order = db.Column(db.Integer, default=0)

    pill = db.relationship('TrainingPill', back_populates='questions')
    translations = db.relationship('TrainingTranslation', back_populates='question',
                                   lazy=True, cascade='all, delete-orphan')

    def translation(self, lang: str):
        """Devuelve la traduccion en ese idioma, o None si no existe."""
        return next((t for t in self.translations if t.lang == lang), None)


class TrainingTranslation(db.Model):
    """Texto y audio de una instruccion formativa en un idioma concreto."""
    __tablename__ = 'training_translation'
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer,
                            db.ForeignKey('training_question.id', name='fk_tt_question'),
                            nullable=False, index=True)
    lang = db.Column(db.String(5), nullable=False, index=True)  # es, ar, en, fr, ro, uk
    text = db.Column(db.Text, nullable=False)
    yes_label = db.Column(db.String(50), nullable=False, default='Sí')
    no_label = db.Column(db.String(50), nullable=False, default='No')
    audio_path = db.Column(db.String(255), nullable=True)
    generated_at = db.Column(db.DateTime, nullable=True, default=datetime.now)

    question = db.relationship('TrainingQuestion', back_populates='translations')

    __table_args__ = (db.UniqueConstraint('question_id', 'lang',
                                          name='uq_training_translation'),)


class TrainingCompletion(db.Model):
    __tablename__ = 'training_completion'
    id = db.Column(db.Integer, primary_key=True)
    pill_id = db.Column(db.Integer, db.ForeignKey('training_pill.id'), nullable=False)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.now)
    completed_at = db.Column(db.DateTime, nullable=True)
    score = db.Column(db.Integer, nullable=True)
    passed = db.Column(db.Boolean, nullable=True)
    answers_json = db.Column(db.Text, nullable=True)
    video_watched = db.Column(db.Boolean, default=False)
    time_spent_seconds = db.Column(db.Integer, nullable=True)
    shuffle_map = db.Column(db.Text, nullable=True)

    pill = db.relationship('TrainingPill', back_populates='completions')
    cleaner = db.relationship('Cleaner', backref=db.backref('training_completions', lazy=True))


class TrainingAssignment(db.Model):
    """Assigns a training pill to a specific worker."""
    __tablename__ = 'training_assignment'
    id = db.Column(db.Integer, primary_key=True)
    pill_id = db.Column(db.Integer, db.ForeignKey('training_pill.id'), nullable=False)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    assigned_at = db.Column(db.DateTime, default=datetime.now)
    assigned_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    pill = db.relationship('TrainingPill', back_populates='assignments')
    cleaner = db.relationship('Cleaner', foreign_keys=[cleaner_id])
    assigner = db.relationship('Cleaner', foreign_keys=[assigned_by])

    __table_args__ = (db.UniqueConstraint('pill_id', 'cleaner_id', name='uq_training_assignment'),)


# ── TURNOS / CUADRANTES ──────────────────────────────────────────────────────

class ShiftType(db.Model):
    __tablename__ = 'shift_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    short_name = db.Column(db.String(5), nullable=False)
    color = db.Column(db.String(7), nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    breaks_minutes = db.Column(db.Integer, default=0)
    sort_order = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)

    assignments = db.relationship('ShiftAssignment', back_populates='shift_type', lazy=True)


class ShiftAssignment(db.Model):
    __tablename__ = 'shift_assignment'
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, index=True)
    shift_type_id = db.Column(db.Integer, db.ForeignKey('shift_type.id'), nullable=True)
    is_override = db.Column(db.Boolean, default=False)
    source = db.Column(db.String(20), default='manual')
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    cleaner = db.relationship('Cleaner', foreign_keys=[cleaner_id],
                              backref=db.backref('shift_assignments', lazy=True))
    shift_type = db.relationship('ShiftType', back_populates='assignments')
    creator = db.relationship('Cleaner', foreign_keys=[created_by])

    __table_args__ = (db.UniqueConstraint('cleaner_id', 'date', name='uq_worker_date'),)

    @property
    def net_hours(self):
        """Effective working hours for this assignment."""
        if not self.shift_type:
            return 0.0
        st = self.shift_type
        from datetime import datetime as _dt, timedelta as _td
        start = _dt.combine(self.date, st.start_time)
        end = _dt.combine(self.date, st.end_time)
        if end <= start:
            end += _td(days=1)  # night shift crosses midnight
        total_min = (end - start).total_seconds() / 60
        return (total_min - (st.breaks_minutes or 0)) / 60


class RotationPattern(db.Model):
    __tablename__ = 'rotation_pattern'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    cycle_days = db.Column(db.Integer, nullable=False, default=7)
    active = db.Column(db.Boolean, default=True)

    days = db.relationship('RotationPatternDay', back_populates='pattern',
                           lazy=True, order_by='RotationPatternDay.day_number',
                           cascade='all, delete-orphan')
    worker_configs = db.relationship('WorkerShiftConfig', back_populates='pattern', lazy=True)


class RotationPatternDay(db.Model):
    __tablename__ = 'rotation_pattern_day'
    id = db.Column(db.Integer, primary_key=True)
    pattern_id = db.Column(db.Integer, db.ForeignKey('rotation_pattern.id'), nullable=False)
    day_number = db.Column(db.Integer, nullable=False)
    shift_type_id = db.Column(db.Integer, db.ForeignKey('shift_type.id'), nullable=True)

    pattern = db.relationship('RotationPattern', back_populates='days')
    shift_type = db.relationship('ShiftType')

    __table_args__ = (db.UniqueConstraint('pattern_id', 'day_number', name='uq_pattern_day'),)


class WorkerShiftConfig(db.Model):
    __tablename__ = 'worker_shift_config'
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    pattern_id = db.Column(db.Integer, db.ForeignKey('rotation_pattern.id'), nullable=True)
    fixed_shift_type_id = db.Column(db.Integer, db.ForeignKey('shift_type.id'), nullable=True)
    cycle_start_date = db.Column(db.Date, nullable=False)
    effective_from = db.Column(db.Date, nullable=False)
    effective_until = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    cleaner = db.relationship('Cleaner', backref=db.backref('shift_configs', lazy=True))
    pattern = db.relationship('RotationPattern', back_populates='worker_configs')
    fixed_shift_type = db.relationship('ShiftType')


class AbsenceType(db.Model):
    __tablename__ = 'absence_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    short_name = db.Column(db.String(5), nullable=False)
    color = db.Column(db.String(7), nullable=False)
    counts_as_worked = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)


class Absence(db.Model):
    __tablename__ = 'absence'
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    absence_type_id = db.Column(db.Integer, db.ForeignKey('absence_type.id'), nullable=False)
    start_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date, nullable=False, index=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    cleaner = db.relationship('Cleaner', foreign_keys=[cleaner_id],
                              backref=db.backref('absences', lazy=True))
    absence_type = db.relationship('AbsenceType')
    creator = db.relationship('Cleaner', foreign_keys=[created_by])


class ShiftCoverageRequirement(db.Model):
    __tablename__ = 'shift_coverage_requirement'
    id = db.Column(db.Integer, primary_key=True)
    shift_type_id = db.Column(db.Integer, db.ForeignKey('shift_type.id'), nullable=False)
    day_type = db.Column(db.String(20), default='all')  # 'all', 'weekday', 'weekend'
    min_workers = db.Column(db.Integer, nullable=False, default=1)
    ideal_workers = db.Column(db.Integer, nullable=True)

    shift_type = db.relationship('ShiftType')

    __table_args__ = (db.UniqueConstraint('shift_type_id', 'day_type', name='uq_coverage_shift_day'),)


# ── RUTAS DE LIMPIEZA ────────────────────────────────────────────────────────

class CleaningZoneAssignment(db.Model):
    """Assigns a worker to specific floors for cleaning."""
    __tablename__ = 'cleaning_zone_assignment'
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    floor_id = db.Column(db.Integer, db.ForeignKey('floor.id'), nullable=False)

    cleaner = db.relationship('Cleaner', backref=db.backref('zone_assignments', lazy=True))
    floor = db.relationship('Floor')

    __table_args__ = (db.UniqueConstraint('cleaner_id', 'floor_id', name='uq_cleaner_floor'),)


class CleaningTargetTime(db.Model):
    """Admin-defined target cleaning time per room type."""
    __tablename__ = 'cleaning_target_time'
    id = db.Column(db.Integer, primary_key=True)
    room_type_id = db.Column(db.Integer, db.ForeignKey('room_type.id'), nullable=False, unique=True)
    target_minutes = db.Column(db.Float, nullable=False, default=15)

    room_type = db.relationship('RoomType')


class DailyCleaningAssignment(db.Model):
    """Persisted per-day room assignment for a worker (manual or auto-generated)."""
    __tablename__ = 'daily_cleaning_assignment'
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, index=True)
    source = db.Column(db.String(20), default='manual')
    position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    cleaner = db.relationship('Cleaner', foreign_keys=[cleaner_id])
    room = db.relationship('Room')
    creator = db.relationship('Cleaner', foreign_keys=[created_by])

    __table_args__ = (db.UniqueConstraint('cleaner_id', 'room_id', 'date', name='uq_daily_clean'),)


# ── ACTIVIDADES ───────────────────────────────────────────────────────────────

class ActivityTemplate(db.Model):
    """Template for recurring activities."""
    __tablename__ = 'activity_template'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Monday, 6=Sunday
    start_time = db.Column(db.Time, nullable=True)
    end_time = db.Column(db.Time, nullable=True)
    location = db.Column(db.String(100), nullable=True)
    category = db.Column(db.String(50), default='general')
    recurrence = db.Column(db.String(20), default='weekly')  # weekly, biweekly, monthly
    resident_ids_json = db.Column(db.Text, nullable=True)  # JSON array of invited resident IDs
    active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    creator = db.relationship('Cleaner', foreign_keys=[created_by])
    activities = db.relationship('Activity', backref='template', lazy=True)


class Activity(db.Model):
    """Scheduled activity (taller, gimnàsia, bingo, etc.)."""
    __tablename__ = 'activity'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    activity_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=True)
    end_time = db.Column(db.Time, nullable=True)
    location = db.Column(db.String(100), nullable=True)
    category = db.Column(db.String(50), default='general')
    template_id = db.Column(db.Integer, db.ForeignKey('activity_template.id'), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    creator = db.relationship('Cleaner', foreign_keys=[created_by])
    participations = db.relationship('ActivityParticipation', backref='activity', lazy=True, cascade='all, delete-orphan')
    photos = db.relationship('ActivityPhoto', backref='activity', lazy=True, cascade='all, delete-orphan')


class ActivityParticipation(db.Model):
    """Records a resident's invitation and participation in an activity."""
    __tablename__ = 'activity_participation'
    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(db.Integer, db.ForeignKey('activity.id'), nullable=False, index=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    invited = db.Column(db.Boolean, default=True)
    engagement = db.Column(db.String(20), nullable=True)
    confirmed_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    resident = db.relationship('Resident', backref=db.backref('activity_participations', lazy=True))
    confirmer = db.relationship('Cleaner', foreign_keys=[confirmed_by])

    __table_args__ = (db.UniqueConstraint('activity_id', 'resident_id', name='uq_activity_resident'),)


class ActivityPhoto(db.Model):
    """Photo taken during an activity."""
    __tablename__ = 'activity_photo'
    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(db.Integer, db.ForeignKey('activity.id'), nullable=False, index=True)
    photo_path = db.Column(db.String(255), nullable=False)
    caption = db.Column(db.Text, nullable=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.now)

    uploader = db.relationship('Cleaner', foreign_keys=[uploaded_by])


# ── ESTADO DE ÁNIMO ──────────────────────────────────────────────────────────

class MoodRecord(db.Model):
    """Mood/behavior check-in for a resident."""
    __tablename__ = 'mood_record'
    id = db.Column(db.Integer, primary_key=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    mood_score = db.Column(db.Integer, nullable=False)  # 1-5
    behavior_flags = db.Column(db.Text, nullable=True)  # JSON array
    notes = db.Column(db.Text, nullable=True)
    recorded_at = db.Column(db.DateTime, default=datetime.now, index=True)

    resident = db.relationship('Resident', backref=db.backref('mood_records', lazy=True))
    worker = db.relationship('Cleaner', foreign_keys=[worker_id])


# ── MEDICACIÓN ────────────────────────────────────────────────────────────────

class MedicationPrescription(db.Model):
    """Active medication prescription for a resident."""
    __tablename__ = 'medication_prescription'
    id = db.Column(db.Integer, primary_key=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    drug_name = db.Column(db.String(150), nullable=False)
    dose = db.Column(db.String(100), nullable=False)
    route = db.Column(db.String(50), default='oral')
    frequency = db.Column(db.String(50), nullable=False)
    schedule_times = db.Column(db.String(200))
    instructions = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, default=True, index=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    prescribed_by = db.Column(db.String(100), nullable=True)
    barcode = db.Column(db.String(100), nullable=True)  # EAN/UPC barcode for verification
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    resident = db.relationship('Resident', backref=db.backref('prescriptions', lazy=True))
    creator = db.relationship('Cleaner', foreign_keys=[created_by])
    administrations = db.relationship('MedicationAdministration', backref='prescription', lazy=True)


class MedicationAdministration(db.Model):
    """Record of a medication being administered to a resident."""
    __tablename__ = 'medication_administration'
    id = db.Column(db.Integer, primary_key=True)
    prescription_id = db.Column(db.Integer, db.ForeignKey('medication_prescription.id'), nullable=False, index=True)
    administered_at = db.Column(db.DateTime, default=datetime.now, index=True)
    administered_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    scheduled_time = db.Column(db.String(5), nullable=True)
    status = db.Column(db.String(20), default='given')
    notes = db.Column(db.Text, nullable=True)

    worker = db.relationship('Cleaner', foreign_keys=[administered_by])


# ── VALORACIONES CLÍNICAS ─────────────────────────────────────────────────────

class AssessmentRecord(db.Model):
    """A single clinical assessment (Barthel, Norton, etc.) for a resident."""
    __tablename__ = 'assessment_record'
    id = db.Column(db.Integer, primary_key=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    scale_type = db.Column(db.String(20), nullable=False, index=True)
    score = db.Column(db.Integer, nullable=False)
    interpretation = db.Column(db.String(50))
    answers_json = db.Column(db.Text)
    notes = db.Column(db.Text, nullable=True)
    assessed_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    assessed_at = db.Column(db.DateTime, default=datetime.now, index=True)

    resident = db.relationship('Resident', backref=db.backref('assessments', lazy=True))
    assessor = db.relationship('Cleaner', foreign_keys=[assessed_by])


# ── INCIDENTES ───────────────────────────────────────────────────────────────

class IncidentType(db.Model):
    __tablename__ = 'incident_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(50), nullable=True)
    color = db.Column(db.String(20), nullable=True)
    severity = db.Column(db.String(20), nullable=False, default='medium')
    active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)


class Incident(db.Model):
    __tablename__ = 'incident'
    id = db.Column(db.Integer, primary_key=True)
    incident_type_id = db.Column(db.Integer, db.ForeignKey('incident_type.id'), nullable=True, index=True)
    reported_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False, index=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(100), nullable=True)
    severity = db.Column(db.String(20), nullable=False, default='medium')
    status = db.Column(db.String(20), nullable=False, default='open')
    occurred_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolved_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    resolution_notes = db.Column(db.Text, nullable=True)

    incident_type = db.relationship('IncidentType', backref=db.backref('incidents', lazy=True))
    reporter = db.relationship('Cleaner', foreign_keys=[reported_by],
                               backref=db.backref('reported_incidents', lazy=True))
    resident = db.relationship('Resident', backref=db.backref('incidents', lazy=True))
    resolver = db.relationship('Cleaner', foreign_keys=[resolved_by])
    is_fall = db.Column(db.Boolean, default=False)
    fall_record = db.relationship('FallRecord', backref='incident', uselist=False, cascade='all, delete-orphan')


class FallRecord(db.Model):
    """Structured fall data linked to an incident."""
    __tablename__ = 'fall_record'
    id = db.Column(db.Integer, primary_key=True)
    incident_id = db.Column(db.Integer, db.ForeignKey('incident.id'), nullable=False, unique=True)
    fall_location = db.Column(db.String(100), nullable=True)
    activity_at_time = db.Column(db.String(100), nullable=True)  # walking, sleeping, transferring...
    footwear = db.Column(db.String(50), nullable=True)  # barefoot, slippers, shoes
    witnesses = db.Column(db.Text, nullable=True)
    injuries = db.Column(db.Text, nullable=True)  # description of injuries
    injury_severity = db.Column(db.String(20), nullable=True)  # none, minor, moderate, severe
    measures_taken = db.Column(db.Text, nullable=True)
    contributing_factors = db.Column(db.Text, nullable=True)  # medication, wet floor, etc
    post_fall_vitals = db.Column(db.Text, nullable=True)


# ── PLANS D'ATENCIÓ ─────────────────────────────────────────────────────────

class CarePlan(db.Model):
    """Individualized care plan (PAI) for a resident."""
    __tablename__ = 'care_plan'
    id = db.Column(db.Integer, primary_key=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    objectives = db.Column(db.Text, nullable=True)  # JSON array
    interventions = db.Column(db.Text, nullable=True)  # JSON array
    html_content = db.Column(db.Text, nullable=True)  # Full AI-generated PAI
    review_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default='active')  # active, reviewed, archived
    ai_review = db.Column(db.Text, nullable=True)  # Latest AI review of progress
    ai_review_date = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)

    resident = db.relationship('Resident', backref=db.backref('care_plans', lazy=True))
    creator = db.relationship('Cleaner', foreign_keys=[created_by])


# ── RESUMS DIARIS ───────────────────────────────────────────────────────────

class DailyDigest(db.Model):
    """AI-generated daily summary for a resident."""
    __tablename__ = 'daily_digest'
    id = db.Column(db.Integer, primary_key=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    html_content = db.Column(db.Text, nullable=False)
    highlights = db.Column(db.Text, nullable=True)  # JSON: key takeaways
    has_alerts = db.Column(db.Boolean, default=False)
    generated_at = db.Column(db.DateTime, default=datetime.now)

    resident = db.relationship('Resident', backref=db.backref('daily_digests', lazy=True))

    __table_args__ = (db.UniqueConstraint('resident_id', 'date', name='uq_daily_digest'),)


# ── REGISTRE NUTRICIONAL ────────────────────────────────────────────────────

class MealRecord(db.Model):
    """Meal/fluid intake record for a resident."""
    __tablename__ = 'meal_record'
    id = db.Column(db.Integer, primary_key=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    meal_type = db.Column(db.String(20), nullable=False)  # breakfast, lunch, snack, dinner
    intake_pct = db.Column(db.Integer, nullable=False)  # 0-100
    fluid_ml = db.Column(db.Integer, nullable=True)
    texture = db.Column(db.String(30), nullable=True)  # normal, soft, puree, liquid
    notes = db.Column(db.Text, nullable=True)
    recorded_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    resident = db.relationship('Resident', backref=db.backref('meal_records', lazy=True))
    worker = db.relationship('Cleaner', foreign_keys=[recorded_by])

    __table_args__ = (db.UniqueConstraint('resident_id', 'date', 'meal_type', name='uq_meal_record'),)


# ── NOTIFICACIONES ──────────────────────────────────────────────────────────

class Notification(db.Model):
    __tablename__ = 'notification'
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(30), nullable=False)  # vital_alert, incident, document_pending, stale_session, coverage_gap
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=True)
    severity = db.Column(db.String(20), default='info')  # critical, warning, info
    link = db.Column(db.String(500), nullable=True)
    read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    # Optional references
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    resident = db.relationship('Resident')
    worker = db.relationship('Cleaner')


class PushSubscription(db.Model):
    """Web Push API subscription for a worker's browser."""
    __tablename__ = 'push_subscription'
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    keys_json = db.Column(db.Text, nullable=False)  # JSON: {p256dh, auth}
    created_at = db.Column(db.DateTime, default=datetime.now)

    worker = db.relationship('Cleaner')


class WoundRecord(db.Model):
    """Track wounds/injuries on a resident's body map."""
    __tablename__ = 'wound_record'
    id = db.Column(db.Integer, primary_key=True)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False, index=True)
    body_zone = db.Column(db.String(50), nullable=False)  # e.g. 'head', 'torso_front', 'left_arm', 'sacrum'
    body_x = db.Column(db.Float, nullable=True)  # % position on SVG (0-100)
    body_y = db.Column(db.Float, nullable=True)
    wound_type = db.Column(db.String(30), nullable=False)  # ulcer, bruise, cut, burn, rash, other
    description = db.Column(db.Text, nullable=True)
    size_cm = db.Column(db.String(20), nullable=True)  # e.g. '2x3'
    severity = db.Column(db.String(20), default='moderate')  # mild, moderate, severe
    status = db.Column(db.String(20), default='active')  # active, healing, healed, worsening
    photo_path = db.Column(db.String(255), nullable=True)
    reported_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, nullable=True)
    healed_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    resident = db.relationship('Resident', backref=db.backref('wounds', lazy=True))
    reporter = db.relationship('Cleaner', foreign_keys=[reported_by])


class WoundUpdate(db.Model):
    """Track wound evolution over time."""
    __tablename__ = 'wound_update'
    id = db.Column(db.Integer, primary_key=True)
    wound_id = db.Column(db.Integer, db.ForeignKey('wound_record.id'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False)  # active, healing, healed, worsening
    size_cm = db.Column(db.String(20), nullable=True)
    description = db.Column(db.Text, nullable=True)
    photo_path = db.Column(db.String(255), nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    wound = db.relationship('WoundRecord', backref=db.backref('updates', lazy=True, order_by='WoundUpdate.created_at.desc()'))
    updater = db.relationship('Cleaner', foreign_keys=[updated_by])


# ── AUDIT TRAIL ─────────────────────────────────────────────────────────────

class AuditLog(db.Model):
    """Records changes to important data for compliance."""
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)
    action = db.Column(db.String(20), nullable=False)  # create, update, delete
    table_name = db.Column(db.String(50), nullable=False)
    record_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.Text, nullable=True)  # JSON: changed fields
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)

    user = db.relationship('Cleaner', foreign_keys=[user_id])
