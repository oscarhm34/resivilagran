"""Shared utilities used across blueprints and routes."""
from __future__ import annotations
from functools import wraps
from datetime import datetime, timedelta, date, time as dt_time

from flask import abort, request, jsonify, redirect, url_for, flash, g
from flask_login import login_required, current_user
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from . import app, db
from .models import (
    Cleaner, Room, Resident, CleaningRecord, CareRecord, CareType,
    AppSetting, CleaningTargetTime, CleaningZoneAssignment,
    AuditLog,
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


def dual_auth(f):
    """Acepta las dos identidades de la app: JWT (PWA) y cookie (panel).

    La mensajeria es la primera funcionalidad que comparten los dos mundos: una
    conversacion tiene de un lado a una trabajadora en la PWA y del otro a una
    gestora en el panel, y la pertenencia a esa conversacion se comprueba igual
    venga de donde venga. Duplicar cada ruta (como hace `chat.py`, donde el
    comportamiento si difiere) daria dos sitios donde olvidarse de comprobarla,
    que es justo lo que sostiene la privacidad de las conversaciones.

    El usuario resuelto queda en `g.dual_user`; se lee con `current_dual_user()`.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = None
        if (request.headers.get('Authorization') or '').startswith('Bearer '):
            try:
                verify_jwt_in_request()
            except Exception:
                return jsonify({'error': 'No autorizado'}), 401
            user = Cleaner.query.filter_by(username=get_jwt_identity()).first()
        elif current_user.is_authenticated:
            user = current_user
            # El blueprint esta exento de CSRF y la cookie viaja sola: sin esto,
            # una pagina externa podria escribir en nombre de quien tenga la
            # sesion abierta. `base.html` ya inyecta la cabecera en los no-GET,
            # y un formulario cross-site no puede ponerla.
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and not (
                    request.headers.get('X-CSRFToken')
                    or request.headers.get('X-Requested-With')):
                return jsonify({'error': 'No autorizado'}), 403

        if not user:
            return jsonify({'error': 'No autorizado'}), 401
        if not user.active:
            return jsonify({'error': 'Tu usuario esta desactivado'}), 403

        g.dual_user = user
        return f(*args, **kwargs)
    return decorated


def current_dual_user():
    """Cleaner resuelto por `dual_auth` en la peticion en curso."""
    return getattr(g, 'dual_user', None)


# ── Redirecciones ────────────────────────────────────────────────────────────

def volver_atras(por_defecto: str) -> str:
    """Destino de vuelta seguro a partir del Referer.

    Volver a `request.referrer` es comodo —deja al administrador donde estaba—
    pero esa cabecera la pone quien enlaza, no el servidor: aceptarla tal cual
    convierte cualquier formulario del panel en un salto a otro sitio, que es la
    mitad del trabajo de una pagina de phishing. Se admite solo una ruta de este
    mismo servidor; cualquier otra cosa cae en el destino por defecto.

    Es la misma comprobacion que ya se hace con el parametro `next` del login.
    """
    from urllib.parse import urlsplit

    ref = request.referrer
    if not ref:
        return por_defecto
    partes = urlsplit(ref)
    if partes.netloc and partes.netloc != urlsplit(request.host_url).netloc:
        return por_defecto
    if not partes.path.startswith('/') or partes.path.startswith('//'):
        return por_defecto
    return partes.path + (f'?{partes.query}' if partes.query else '')


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _current_worker():
    """Cleaner correspondiente a la identidad del JWT, o None."""
    return Cleaner.query.filter_by(username=get_jwt_identity()).first()


def _current_worker_id():
    """ID del Cleaner autenticado por JWT, o None.

    Se usa en los endpoints de la PWA en lugar del `worker_id` que envía el
    cliente: ese parámetro es manipulable y permitiría operar sobre los datos
    de otra trabajadora. Los endpoints lo siguen aceptando por compatibilidad
    con `worker.html`, pero lo ignoran.
    """
    worker = _current_worker()
    return worker.id if worker else None


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


def _safe_flush(error_msg='Error al guardar en la base de datos'):
    """Like _safe_commit but only flushes, so autogenerated IDs are available
    for log_audit() before the real commit. Returns (success, error_message)."""
    try:
        db.session.flush()
        return True, None
    except IntegrityError as e:
        db.session.rollback()
        app.logger.warning('IntegrityError en flush: %s', e)
        return False, 'Conflicto de datos: registro duplicado o referencia inválida.'
    except OperationalError as e:
        db.session.rollback()
        app.logger.error('OperationalError en flush: %s', e)
        return False, 'Error de base de datos. Inténtalo de nuevo.'
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error('SQLAlchemyError en flush: %s', e)
        return False, error_msg


# ── File helpers ─────────────────────────────────────────────────────────────

ALLOWED_DOC_EXTENSIONS = {'pdf', 'txt', 'csv', 'doc', 'docx', 'jpg', 'jpeg', 'png', 'webp'}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}


def _allowed_file(filename: str, allowed: set) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


# Audio y video: la extension se deriva del MIME contra este mapa cerrado, nunca
# del nombre que manda el cliente. Sin ffmpeg no se puede reprocesar el fichero
# como se hace con las imagenes, asi que la defensa son tres capas: mapa cerrado,
# firma binaria (`_sniff_media`) y, al servirlo, Content-Type de lista blanca con
# `X-Content-Type-Options: nosniff`.
AUDIO_MIME_EXTENSIONS = {
    'audio/webm': 'webm',
    'audio/ogg': 'ogg',
    'audio/mp4': 'm4a',
    'audio/x-m4a': 'm4a',
    'audio/mpeg': 'mp3',
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
}
VIDEO_MIME_EXTENSIONS = {
    'video/mp4': 'mp4',
    'video/webm': 'webm',
    'video/quicktime': 'mov',
}
ALLOWED_AUDIO_EXTENSIONS = set(AUDIO_MIME_EXTENSIONS.values())
ALLOWED_VIDEO_EXTENSIONS = set(VIDEO_MIME_EXTENSIONS.values())


def _sniff_media(header: bytes, media_type: str) -> bool:
    """Comprueba la firma binaria de un audio o un video.

    Un `.webm` que en realidad es un HTML con JavaScript dentro es el ataque
    obvio contra un almacen de ficheros que no se reprocesan. Mirar los primeros
    bytes no es infalible, pero descarta el fichero disfrazado, que es el caso
    real. La defensa que de verdad cierra el agujero es servirlo con `nosniff`.
    """
    if len(header) < 12:
        return False
    ebml = bytes.fromhex("1a45dfa3")          # webm / matroska
    mp3_raw = (bytes.fromhex("fffb"), bytes.fromhex("fff3"), bytes.fromhex("fff2"))
    if media_type == "audio":
        return (
            header.startswith(ebml)
            or header.startswith(b"OggS")             # ogg / opus
            or header[4:8] == b"ftyp"                 # mp4 / m4a
            or header.startswith(b"ID3")              # mp3 con etiquetas
            or header[:2] in mp3_raw                  # mp3 crudo
            or (header.startswith(b"RIFF") and header[8:12] == b"WAVE")
        )
    if media_type == "video":
        return header.startswith(ebml) or header[4:8] == b"ftyp"
    return False


def _save_image_stream(source, folder: str, basename: str,
                       max_side: int = 1280, quality: int = 82) -> tuple:
    """Reprocesa una imagen con Pillow y la guarda como JPEG.

    Ese reprocesado (abrir, corregir EXIF, convertir a RGB, redimensionar y
    reescribir) es lo que descarta cualquier carga util escondida en el fichero
    original, asi que es la defensa antifichero-malicioso de todo lo que sube un
    usuario. Vive aqui para que no haya dos copias que endurecer por separado:
    `_save_base64_photo` de `blueprints/nfc.py` tambien la usa.

    Devuelve (nombre_de_fichero, ancho, alto).
    """
    import os
    from PIL import Image  # noqa: F401  (se usa a traves de _open_image_oriented)

    img = _open_image_oriented(source)
    img = img.convert('RGB')
    img.thumbnail((max_side, max_side))
    os.makedirs(folder, exist_ok=True)
    filename = f'{basename}.jpg'
    img.save(os.path.join(folder, filename), 'JPEG', quality=quality, optimize=True)
    return filename, img.width, img.height


def _open_image_oriented(source):
    """Abre una imagen aplicando la orientacion EXIF de la camara.

    Los moviles guardan el JPEG sin rotar y anotan la orientacion en EXIF.
    Pillow no la aplica sola, y thumbnail()/save() descartan el bloque EXIF,
    asi que hay que transponer los pixeles antes de redimensionar o la foto
    queda girada 90 grados en disco.
    """
    from PIL import Image, ImageOps
    return ImageOps.exif_transpose(Image.open(source))


# ── Idiomas de las pildoras formativas ───────────────────────────────────────

TRAINING_LANGUAGES = {
    'es': {'name': 'Espanol', 'native': 'Español', 'flag': '🇪🇸', 'voice': 'nova', 'rtl': False},
    'ar': {'name': 'Arabe', 'native': 'العربية', 'flag': '🇸🇦', 'voice': 'nova', 'rtl': True},
    'en': {'name': 'Ingles', 'native': 'English', 'flag': '🇬🇧', 'voice': 'nova', 'rtl': False},
    'fr': {'name': 'Frances', 'native': 'Français', 'flag': '🇫🇷', 'voice': 'nova', 'rtl': False},
    'ro': {'name': 'Rumano', 'native': 'Română', 'flag': '🇷🇴', 'voice': 'nova', 'rtl': False},
    'uk': {'name': 'Ucraniano', 'native': 'Українська', 'flag': '🇺🇦', 'voice': 'nova', 'rtl': False},
}


# ── Formatting helpers ───────────────────────────────────────────────────────

def _format_duration(start_time: datetime | None, end_time: datetime | None) -> str:
    if start_time and end_time:
        seconds = int((end_time - start_time).total_seconds())
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f'{hours:02d}:{minutes:02d}:{secs:02d}'
    return 'N/A'


# ── Duracion minima de una sesion ────────────────────────────────────────────

MIN_SESSION_SECONDS_DEFAULT = '60'


def _falta_para_cerrar(start_time) -> int:
    """Segundos que faltan para poder cerrar la sesion. 0 = ya se puede.

    Sin esto, abrir y cerrar en dos segundos deja un registro de limpieza o de
    atencion que alimenta las estadisticas y los informes como si fuera trabajo
    real. El propio sistema ya daba por hecho que eso no cuenta: las medias de
    `_compute_cleaning_stats` descartan lo que dura menos de un minuto.

    Con `start_time` a None devuelve 0: el de CleaningRecord es nullable, y
    bloquear un registro cuyo inicio no se conoce lo dejaria imposible de cerrar.
    """
    minimo = int(AppSetting.get('min_session_seconds', MIN_SESSION_SECONDS_DEFAULT))
    if minimo <= 0 or not start_time:
        return 0
    transcurrido = (datetime.now() - start_time).total_seconds()
    # Hacia arriba: con `round`, cuando quedan 400 ms el aviso diria "faltan 0:00"
    # y el siguiente intento tambien fallaria.
    import math
    return max(0, int(math.ceil(minimo - transcurrido)))


def _aviso_falta(segundos: int) -> str:
    """El texto que ve la trabajadora. Dice cuanto falta, no la rine."""
    minutos, secs = divmod(max(0, segundos), 60)
    return f'Aún no puedes finalizar. Faltan {minutos}:{secs:02d}.'


def _tipos_de_atencion_a_esta_hora(momento) -> list:
    """Tipos de atencion activos cuya franja cubre esa hora.

    Hay atenciones que se hacen siempre a la misma hora —levantar por la manana,
    acostar por la noche— y el sistema no lo sabia: al terminar, la trabajadora
    elegia el tipo de una lista donde todo pesa igual.

    Sin franja configurada un tipo no se sugiere nunca, que es como se comportan
    todos los que ya existen.

    Si `start_time` es mayor que `end_time` la franja **cruza la medianoche**
    (21:00 a 01:00). Sin esto, "Acostar" habria que partirlo en dos tramos o
    terminarlo antes de las doce.
    """
    hora = momento.time()
    coinciden = []
    for ct in CareType.query.filter(
            CareType.active.is_(True),
            CareType.start_time.isnot(None),
            CareType.end_time.isnot(None)).order_by(CareType.sort_order, CareType.name).all():
        if ct.start_time <= ct.end_time:
            dentro = ct.start_time <= hora <= ct.end_time
        else:
            dentro = hora >= ct.start_time or hora <= ct.end_time
        if dentro:
            coinciden.append(ct)
    return coinciden


# ── Texto a voz ──────────────────────────────────────────────────────────────

TTS_MAX_CHARS = 2000
TTS_MODELO = 'gpt-4o-mini-tts'
TTS_VOZ = 'nova'


def _tts_mp3(texto: str, voz: str = TTS_VOZ):
    """Llama al servicio de voz y devuelve (bytes_mp3, error).

    Lo usan tanto la formacion (varios idiomas, una voz por idioma) como la
    informacion del residente, para no tener dos veces la misma peticion.
    """
    import requests

    api_key = app.config.get('OPENAI_API_KEY')
    if not api_key:
        return None, 'El audio no está configurado'

    try:
        res = requests.post(
            'https://api.openai.com/v1/audio/speech',
            headers={'Authorization': f'Bearer {api_key}'},
            json={'model': TTS_MODELO, 'voice': voz,
                  'input': texto, 'response_format': 'mp3'},
            timeout=60,
        )
    except requests.RequestException as e:
        app.logger.error('Error de red al generar el audio: %s', e)
        return None, 'No se ha podido generar el audio'

    if res.status_code != 200:
        app.logger.error('El servicio de voz devolvio %s', res.status_code)
        return None, 'No se ha podido generar el audio'

    return res.content, None


def _audio_de_texto(texto: str, subcarpeta: str, prefijo: str):
    """Devuelve la ruta relativa del mp3 de ese texto, generandolo si hace falta.

    El nombre lleva un hash del texto, y ahi esta la diferencia con lo que hace
    formacion: alli, al editar el texto se borra el audio y queda "Sin audio"
    hasta que un administrador pulsa el boton de generar. Con el hash, un texto
    distinto es un fichero distinto, asi que **el audio nunca puede desafinar con
    el texto**: se regenera solo la primera vez que alguien le da a escuchar.

    Devuelve (ruta_relativa, error). El error es un mensaje en castellano listo
    para el usuario, o None si fue bien.
    """
    import hashlib
    import os

    texto = (texto or '').strip()
    if not texto:
        return None, 'No hay texto que leer'

    if not _hay_voz():
        return None, 'El audio no está configurado'

    # El tope existe porque no hay ningun control de gasto en el proyecto: sin el,
    # un texto pegado de tres folios se cobra entero cada vez que cambie.
    recortado = texto[:TTS_MAX_CHARS]
    firma = hashlib.sha256(recortado.encode('utf-8')).hexdigest()[:8]
    nombre = f'{prefijo}_{firma}.mp3'
    carpeta = os.path.join(app.config['UPLOAD_FOLDER'], subcarpeta)
    os.makedirs(carpeta, exist_ok=True)
    destino = os.path.join(carpeta, nombre)
    relativa = f'{subcarpeta}/{nombre}'

    if os.path.exists(destino):
        return relativa, None

    contenido, error = _tts_mp3(recortado)
    if error:
        return None, error

    try:
        with open(destino, 'wb') as fh:
            fh.write(contenido)
    except OSError as e:
        app.logger.error('Error al guardar el audio: %s', e)
        return None, 'No se ha podido guardar el audio'

    # Los de versiones anteriores de este mismo texto ya no valen para nada.
    for viejo in os.listdir(carpeta):
        if viejo.startswith(f'{prefijo}_') and viejo != nombre:
            try:
                os.remove(os.path.join(carpeta, viejo))
            except OSError:
                pass

    return relativa, None


def _hay_voz() -> bool:
    """Si no hay clave, mejor no pintar un boton que siempre va a fallar."""
    return bool(app.config.get('OPENAI_API_KEY'))


def _today_range(target_date=None):
    """Return (start_datetime, end_datetime) for a given date (default today)."""
    d = target_date or date.today()
    return datetime.combine(d, datetime.min.time()), datetime.combine(d + timedelta(days=1), datetime.min.time())


# ── Parsing helpers ──────────────────────────────────────────────────────────

def _parse_hhmm(value, default: dt_time | None = None) -> dt_time | None:
    """Parse 'HH:MM' or 'HH:MM:SS' into a time. Returns default if empty/invalid.

    An empty <input type="time"> submits '', so request.form.get(k, 'HH:MM')
    never falls back to its default. Never raises.
    """
    if not value:
        return default
    parts = str(value).strip().split(':')
    if len(parts) < 2:
        return default
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return dt_time(hour, minute)


def _parse_iso_date(value, default: date | None = None) -> date | None:
    """Parse an ISO 'YYYY-MM-DD' string into a date. Returns default if invalid."""
    if not value:
        return default
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return default


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


def log_audit(action, table_name, record_id=None, details=None):
    """Record an audit log entry."""
    import json
    user_id = current_user.id if current_user and hasattr(current_user, 'id') else None
    ip = request.remote_addr if request else None
    entry = AuditLog(
        user_id=user_id, action=action,
        table_name=table_name, record_id=record_id,
        details=json.dumps(details, ensure_ascii=False) if details else None,
        ip_address=ip,
    )
    db.session.add(entry)
