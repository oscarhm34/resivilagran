"""Shared utilities used across blueprints and routes."""
from __future__ import annotations
from functools import wraps
from datetime import datetime, timedelta, date

from flask import abort, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user
from flask_jwt_extended import get_jwt_identity
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from . import app, db
from .models import (
    Cleaner, Room, Resident, CleaningRecord, CareRecord,
    AppSetting, CleaningTargetTime, CleaningZoneAssignment,
)


# ── Decorators ───────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _verify_worker_id(worker_id):
    """Verify that the supplied worker_id matches the JWT identity."""
    identity = get_jwt_identity()
    worker = Cleaner.query.filter_by(username=identity).first()
    if not worker or worker.id != worker_id:
        return None
    return worker_id


# ── DB helpers ───────────────────────────────────────────────────────────────

def _safe_commit(error_msg='Error al guardar en la base de datos'):
    """Commit with rollback on failure. Returns (success, error_message)."""
    try:
        db.session.commit()
        return True, None
    except IntegrityError as e:
        db.session.rollback()
        app.logger.warning('IntegrityError: %s', e)
        return False, 'Conflicto de datos: registro duplicado o referencia inválida.'
    except OperationalError as e:
        db.session.rollback()
        app.logger.error('OperationalError: %s', e)
        return False, 'Error de base de datos. Inténtalo de nuevo.'
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error('SQLAlchemyError: %s', e)
        return False, error_msg


# ── File helpers ─────────────────────────────────────────────────────────────

ALLOWED_DOC_EXTENSIONS = {'pdf', 'txt', 'csv', 'doc', 'docx', 'jpg', 'jpeg', 'png', 'webp'}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}


def _allowed_file(filename: str, allowed: set) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


# ── Formatting helpers ───────────────────────────────────────────────────────

def _format_duration(start_time: datetime | None, end_time: datetime | None) -> str:
    if start_time and end_time:
        seconds = int((end_time - start_time).total_seconds())
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f'{hours:02d}:{minutes:02d}:{secs:02d}'
    return 'N/A'


def _today_range(target_date=None):
    """Return (start_datetime, end_datetime) for a given date (default today)."""
    d = target_date or date.today()
    return datetime.combine(d, datetime.min.time()), datetime.combine(d + timedelta(days=1), datetime.min.time())


# ── NFC helpers ──────────────────────────────────────────────────────────────

def _resolve_nfc_code(nfc_code):
    """Resolve NFC code to (Room, Resident) trying padded variants."""
    room = Room.query.filter_by(number=nfc_code).first()
    resident = Resident.query.filter_by(nfc_code=nfc_code, active=True).first()
    if not room and not resident and nfc_code.isdigit():
        padded = [nfc_code.zfill(4), nfc_code.zfill(5)]
        room = Room.query.filter(Room.number.in_(padded)).first()
        resident = Resident.query.filter(Resident.nfc_code.in_(padded), Resident.active == True).first()
    return room, resident


def _check_single_session_conflict(worker_id):
    """Return True if single_session is enabled and worker has an active session."""
    if AppSetting.get('single_session', 'false') != 'true':
        return False
    return bool(
        CleaningRecord.query.filter_by(cleaner_id=worker_id, end_time=None).first() or
        CareRecord.query.filter_by(worker_id=worker_id, end_time=None).first()
    )


# ── Cleaning helpers ─────────────────────────────────────────────────────────

def _calculate_room_urgency(room_id, room_clean_count, room_last_cleaned, now,
                            is_resident_room=False, is_occupied=True):
    """Calculate cleaning urgency ratio for a room.
    Returns (urgency, days_since, expected_freq_days)."""
    count = room_clean_count.get(room_id, 0)
    last = room_last_cleaned.get(room_id)
    expected_freq_days = (90 / count) if count > 0 else 7
    if is_resident_room and not is_occupied:
        expected_freq_days *= 3
    if last:
        days_since = (now - last).days
        urgency = days_since / expected_freq_days if expected_freq_days > 0 else 0
    else:
        days_since = None
        urgency = 10
    return urgency, days_since, expected_freq_days


def _urgency_priority(urgency, cleaned_today=False):
    """Return priority string from urgency ratio."""
    if cleaned_today:
        return 'done'
    if urgency >= 2:
        return 'urgent'
    if urgency >= 1:
        return 'due'
    return 'ok'


def _compute_cleaning_stats(days_back: int = 90) -> dict:
    """Compute average cleaning duration per room from historical data."""
    cutoff = datetime.now() - timedelta(days=days_back)
    records = CleaningRecord.query.filter(
        CleaningRecord.end_time.isnot(None),
        CleaningRecord.start_time >= cutoff,
    ).all()

    room_durations: dict[int, list[float]] = {}
    worker_sequences: dict[tuple[int, str], list[tuple[float, int]]] = {}

    for r in records:
        dur = r.calculate_duration()
        if dur and dur > 60 and dur < 7200:
            mins = dur / 60
            room_durations.setdefault(r.room_id, []).append(mins)
            day_key = (r.cleaner_id, r.start_time.strftime('%Y-%m-%d'))
            worker_sequences.setdefault(day_key, []).append((r.start_time.timestamp(), r.room_id))

    avg_per_room = {}
    for room_id, durs in room_durations.items():
        avg_per_room[room_id] = round(sum(durs) / len(durs), 1)

    transition_counts: dict[tuple[int, int], int] = {}
    for day_key, seq in worker_sequences.items():
        seq.sort()
        room_order = [room_id for _, room_id in seq]
        for i in range(len(room_order) - 1):
            pair = (room_order[i], room_order[i + 1])
            transition_counts[pair] = transition_counts.get(pair, 0) + 1

    return {
        'avg_per_room': avg_per_room,
        'transition_counts': transition_counts,
        'room_durations': room_durations,
    }
