from __future__ import annotations
from . import db
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask_login import UserMixin
from sqlalchemy import extract, func


cleaner_groups = db.Table(
    'cleaner_groups',
    db.Column('cleaner_id', db.Integer, db.ForeignKey('cleaner.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('resident_group.id'), primary_key=True),
)


class Cleaner(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)

    groups = db.relationship('ResidentGroup', secondary=cleaner_groups, back_populates='workers', lazy=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class CleaningRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    room_id = db.Column(db.Integer, nullable=False)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)

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

    group = db.relationship('ResidentGroup', back_populates='residents')
    care_records = db.relationship('CareRecord', back_populates='resident', lazy=True)


class CareType(db.Model):
    __tablename__ = 'care_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

    care_records = db.relationship('CareRecord', back_populates='care_type', lazy=True)


class CareRecord(db.Model):
    __tablename__ = 'care_record'
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    resident_id = db.Column(db.Integer, db.ForeignKey('resident.id'), nullable=False)
    care_type_id = db.Column(db.Integer, db.ForeignKey('care_type.id'), nullable=True)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    worker = db.relationship('Cleaner', backref=db.backref('care_records', lazy=True))
    resident = db.relationship('Resident', back_populates='care_records')
    care_type = db.relationship('CareType', back_populates='care_records')

    def calculate_duration(self) -> float | None:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None
