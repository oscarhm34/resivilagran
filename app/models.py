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
    password_hash = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    identity_verified = db.Column(db.Boolean, nullable=False, default=False)
    role = db.Column(db.String(20), nullable=False, default='atenciones')  # 'limpieza', 'atenciones', 'mixto', 'gestion'

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
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=True)

    questions = db.relationship('TrainingQuestion', back_populates='pill', lazy=True,
                                order_by='TrainingQuestion.sort_order')
    completions = db.relationship('TrainingCompletion', back_populates='pill', lazy=True)
    creator = db.relationship('Cleaner', foreign_keys=[created_by])


class TrainingQuestion(db.Model):
    __tablename__ = 'training_question'
    id = db.Column(db.Integer, primary_key=True)
    pill_id = db.Column(db.Integer, db.ForeignKey('training_pill.id'), nullable=False)
    question_text = db.Column(db.String(500), nullable=False)
    option_a = db.Column(db.String(200), nullable=False)
    option_b = db.Column(db.String(200), nullable=False)
    option_c = db.Column(db.String(200), nullable=False)
    option_d = db.Column(db.String(200), nullable=False)
    correct_option = db.Column(db.String(1), nullable=False)
    sort_order = db.Column(db.Integer, default=0)

    pill = db.relationship('TrainingPill', back_populates='questions')


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
