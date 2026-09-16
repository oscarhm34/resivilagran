"""Shared utilities used across blueprints and routes."""
from __future__ import annotations
import re
from functools import wraps
from datetime import datetime, timedelta, date, time as dt_time

from flask import abort, request, jsonify, redirect, url_for, flash, g
from flask_login import login_required, current_user
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import joinedload

from . import app, db
from .models import (
    Cleaner, Room, Resident, CleaningRecord, CareRecord, CareType,
    AppSetting, CleaningTargetTime, CleaningZoneAssignment,
    AuditLog, ChecklistItem, RoomType, ContentTranslation,
    WorkerDevice, WorkerDeviceUse, WorkerDeviceLogin,
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


# ── Idiomas de la webapp de trabajadoras ─────────────────────────────────────

# Un subconjunto de los de formacion, no una lista aparte: banderas, nombres
# nativos y marca de derecha-a-izquierda ya estan ahi y no hay por que tenerlos
# dos veces. Se anade una entrada aqui cuando la webapp este traducida a ese
# idioma, no cuando lo este una pildora formativa.
APP_LANGUAGES = {code: TRAINING_LANGUAGES[code] for code in ('es', 'ar', 'fr', 'en')}

# Codigo BCP-47 para `toLocaleDateString` y el reconocimiento de voz del movil.
APP_LOCALES = {'es': 'es-ES', 'ar': 'ar-MA', 'fr': 'fr-FR', 'en': 'en-GB'}


def _idioma_valido(code: str | None) -> str:
    """Normaliza un codigo de idioma a uno de los que la webapp sabe hablar."""
    return code if code in APP_LANGUAGES else 'es'


# ── Tono del aviso de mensaje ────────────────────────────────────────────────

# El sonido se sintetiza en el movil (ver `TONOS` en worker.html): aqui solo
# vive el patron de vibracion, que es lo que el servidor tiene que meter en el
# aviso push. Con la aplicacion cerrada Android decide el sonido y la web no
# puede cambiarlo; la vibracion si viaja en el aviso, asi que es lo unico que
# distingue un tono de otro cuando el movil esta en el bolsillo.
TONOS_AVISO = {
    'suave':   [200, 100, 200],
    'campana': [400],
    'triple':  [100, 60, 100, 60, 100],
    'grave':   [300],
}

TONO_AVISO_DEFECTO = 'suave'


def _tono_valido(tono: str | None) -> str:
    """Normaliza un tono a uno de los que la webapp sabe tocar."""
    return tono if tono in TONOS_AVISO else TONO_AVISO_DEFECTO


def _vibracion_de_tono(tono: str | None) -> list:
    """Patron de vibracion del tono, para el aviso push."""
    return TONOS_AVISO[_tono_valido(tono)]


# ── Dispositivos de las trabajadoras ───────────────────────────────────────────

# Un identificador que genera el navegador y guarda en localStorage. Llega en la
# cabecera `X-Device-Id` de todas las peticiones de la webapp. Como en `/login`
# viaja SIN autenticar, se valida el formato antes de tocar la base de datos.
_DEVICE_UID_RE = re.compile(r'^[A-Za-z0-9-]{8,64}$')

# Prefijos comerciales de los modelos que da el User-Agent de Android. Lo que no
# este aqui se ensena tal cual venga: es mejor un "SM-X999" sin traducir que una
# marca inventada.
_MARCAS_ANDROID = (
    ('SM-', 'Samsung'),
    ('GT-', 'Samsung'),
    ('Redmi', 'Xiaomi'),
    ('POCO', 'Xiaomi'),
    ('Mi ', 'Xiaomi'),
    ('CPH', 'Oppo'),
    ('RMX', 'Realme'),
    ('VOG-', 'Huawei'),
    ('ANE-', 'Huawei'),
    ('STK-', 'Huawei'),
    ('moto', 'Motorola'),
    ('Pixel', 'Google'),
    ('Nokia', 'Nokia'),
    ('TCL', 'TCL'),
    ('Infinix', 'Infinix'),
)

# Nombre comercial de los modelos que hay en la residencia. El User-Agent solo
# da el codigo interno ("SM-A546B") y ese codigo no se lo sabe nadie; con el
# nombre de venta, coordinacion reconoce el telefono que tiene en la mano.
# Ampliable segun entren moviles nuevos.
_MODELOS_CONOCIDOS = {
    'SM-A546B': 'Samsung Galaxy A54',
    'SM-A556B': 'Samsung Galaxy A55',
    'SM-A536B': 'Samsung Galaxy A53',
    'SM-A526B': 'Samsung Galaxy A52',
    'SM-A146B': 'Samsung Galaxy A14',
    'SM-A156B': 'Samsung Galaxy A15',
    'SM-A166B': 'Samsung Galaxy A16',
    'SM-A047F': 'Samsung Galaxy A04s',
    'SM-A135F': 'Samsung Galaxy A13',
    'SM-G991B': 'Samsung Galaxy S21',
    'SM-S911B': 'Samsung Galaxy S23',
    'SM-T500': 'Samsung Galaxy Tab A7',
    # Xiaomi solo manda el codigo interno; este es el que hay en la casa.
    '23053RN02Y': 'Xiaomi Redmi 12',
    'SM-X200': 'Samsung Galaxy Tab A8',
}

# Orden importante: Edge y Samsung Internet se hacen pasar por Chrome, y Chrome
# se hace pasar por Safari. El primero que encaje manda.
_NAVEGADORES = (
    ('Edg/', 'Edge'),
    ('EdgA/', 'Edge'),
    ('OPR/', 'Opera'),
    ('SamsungBrowser/', 'Samsung Internet'),
    ('Firefox/', 'Firefox'),
    ('FxiOS/', 'Firefox'),
    ('CriOS/', 'Chrome'),
    ('Chrome/', 'Chrome'),
    ('Version/', 'Safari'),
)


def _nombrar_modelo(codigo: str) -> str:
    """Antepone la marca a un codigo de modelo que no esta en la tabla."""
    for prefijo, marca in _MARCAS_ANDROID:
        if codigo.upper().startswith(prefijo.upper()):
            if codigo.lower().startswith(marca.lower()):
                return codigo
            return '%s %s' % (marca, codigo)
    # Xiaomi numera sus modelos con el ano y el mes de salida: "23078RKD5G" es
    # de julio de 2023. No dice que telefono es, pero al menos dice la marca.
    if re.match(r'^2[0-9]{3,6}[A-Z]', codigo):
        return 'Xiaomi %s' % codigo
    return codigo


def _modelo_android(ua: str) -> str | None:
    """Saca el modelo del User-Agent de un Android.

    Viene dentro del parentesis, despues de "Linux" y de la version del sistema:
    "(Linux; Android 14; SM-A546B)" o "(Linux; Android 11; Redmi Note 10
    Build/RKQ1...)". Se descartan los trozos que no son un modelo (el idioma, la
    palabra `wv` de las webviews) para no acabar ensenando "es-es" como telefono.
    """
    m = re.search(r'\(([^)]*)\)', ua)
    if not m:
        return None
    trozos = [t.strip() for t in m.group(1).split(';')]
    for trozo in trozos[2:]:            # 0 = "Linux", 1 = "Android X"
        trozo = trozo.split('Build/')[0].strip()
        # 'wv' es el WebView. 'K' es lo que manda Chrome desde la version 110,
        # que dejo de decir el modelo: todos los Android del mundo dicen "K".
        # Por eso el modelo de verdad llega aparte, en `X-Device-Model`.
        if not trozo or trozo.lower() == 'wv' or trozo == 'K':
            continue
        if re.match(r'^[a-z]{2}([-_][A-Za-z]{2})?$', trozo):   # "es-es", "ca"
            continue
        return _MODELOS_CONOCIDOS.get(trozo) or _nombrar_modelo(trozo)
    return None


def _describir_dispositivo(ua: str | None, modelo_cliente: str | None = None) -> tuple:
    """Interpreta un User-Agent y devuelve (modelo, sistema, navegador).

    `modelo_cliente` llega en la cabecera `X-Device-Model` y MANDA sobre lo que
    diga el User-Agent. Hace falta porque desde Chrome 110 Android ya no manda
    el modelo ahi: envia "Android 10; K" para todos los telefonos, sean lo que
    sean. El modelo real solo se consigue pidiendolo con Client Hints desde el
    navegador, que es lo que hace la webapp.

    Devuelve `(None, None, None)` con lo que no reconoce, a proposito: es
    preferible que el panel diga "Dispositivo desconocido" y ensene el
    User-Agent crudo a que se invente una marca.

    Werkzeug 3 ya no parsea el User-Agent y no hay libreria para ello en el
    proyecto; anadir una obligaria a un rebuild en el NAS por muy poco.
    """
    codigo = (modelo_cliente or '').strip()[:60]
    if not ua and not codigo:
        return None, None, None

    ua = ua or ''
    modelo = sistema = navegador = None

    m = re.search(r'Android\s+([\d.]+)', ua)
    if m:
        sistema = 'Android %s' % m.group(1)
        modelo = _modelo_android(ua)
    elif 'iPhone' in ua or 'iPad' in ua or 'iPod' in ua:
        # El User-Agent de Apple nunca dice el modelo: todos los iPhone dicen
        # "iPhone" y punto. Es una limitacion del navegador, no del parser.
        modelo = 'iPad' if 'iPad' in ua else 'iPhone'
        m = re.search(r'OS\s+(\d+)[_.](\d+)', ua)
        sistema = 'iOS %s.%s' % (m.group(1), m.group(2)) if m else 'iOS'
    elif 'Windows NT' in ua:
        modelo = 'Ordenador'
        sistema = 'Windows 10 u 11' if 'Windows NT 10.0' in ua else 'Windows'
    elif 'Mac OS X' in ua:
        modelo = 'Ordenador'
        sistema = 'macOS'
    elif 'CrOS' in ua:
        modelo = 'Ordenador'
        sistema = 'ChromeOS'
    elif 'Linux' in ua or 'X11' in ua:
        modelo = 'Ordenador'
        sistema = 'Linux'

    for marca, nombre in _NAVEGADORES:
        if marca not in ua:
            continue
        if nombre == 'Safari' and 'Safari' not in ua:
            continue
        m = re.search(re.escape(marca) + r'(\d+)', ua)
        navegador = '%s %s' % (nombre, m.group(1)) if m else nombre
        break

    # El modelo que da el navegador por Client Hints es el bueno: el del
    # User-Agent, cuando lo hay, es un resto de antes de Chrome 110.
    if codigo:
        modelo = _MODELOS_CONOCIDOS.get(codigo) or _nombrar_modelo(codigo)

    if not (modelo or sistema or navegador):
        return None, None, None
    return modelo, sistema, navegador


def _device_uid_valido(uid):
    """Filtra el identificador que manda el navegador. None si no vale."""
    uid = (uid or '').strip()
    return uid if _DEVICE_UID_RE.match(uid) else None


def _registrar_dispositivo(worker_id, device_uid, user_agent, es_login=False,
                           modelo_cliente=None):
    """Anota que `worker_id` esta usando este movil. Devuelve si escribio algo.

    NO hace commit: lo deja preparado en la sesion para que lo cierre quien
    llama, que en el `after_request` ya va a escribir de todos modos.

    `es_login` distingue un inicio de sesion de una peticion cualquiera: el
    contador cuenta inicios de sesion, no peticiones.
    """
    uid = _device_uid_valido(device_uid)
    if not uid:
        return False

    ahora = datetime.now()
    ua = (user_agent or '')[:500]
    device = WorkerDevice.query.filter_by(device_uid=uid).first()
    if device is None:
        modelo, sistema, navegador = _describir_dispositivo(ua, modelo_cliente)
        device = WorkerDevice(device_uid=uid, user_agent=ua, model=modelo,
                              os_name=sistema, browser=navegador,
                              first_seen=ahora, last_seen=ahora)
        db.session.add(device)
        db.session.flush()          # hace falta el id para la fila de uso
    else:
        device.last_seen = ahora
        # En cada entrada se vuelve a leer quien es este telefono: asi un movil
        # ya dado de alta se arregla solo cuando se anade su codigo a la tabla de
        # modelos, sin tener que tocar la base de datos a mano.
        if es_login:
            modelo, sistema, navegador = _describir_dispositivo(ua, modelo_cliente)
            if ua:
                device.user_agent = ua
            # Campo a campo y sin pisar lo bueno con un hueco. Los Client Hints
            # se piden de forma asincrona, asi que una entrada puede llegar sin
            # el modelo aunque el telefono si lo tenga: sobreescribir a ciegas
            # dejaba en blanco un modelo que ya se sabia.
            device.model = modelo or device.model
            device.os_name = sistema or device.os_name
            device.browser = navegador or device.browser

    uso = WorkerDeviceUse.query.filter_by(device_id=device.id,
                                          worker_id=worker_id).first()
    if uso is None:
        db.session.add(WorkerDeviceUse(device_id=device.id, worker_id=worker_id,
                                       last_used=ahora, login_count=1))
    else:
        uso.last_used = ahora
        if es_login:
            uso.login_count = (uso.login_count or 0) + 1

    # El resumen de arriba dice quien lo lleva hoy; esto deja constancia de quien
    # lo llevaba hace dos semanas, que con los moviles rotando es una pregunta
    # que el resumen ya no puede contestar.
    if es_login:
        db.session.add(WorkerDeviceLogin(device_id=device.id, worker_id=worker_id,
                                         at=ahora))
    return True


def _ultimo_movil_por_trabajadora() -> dict:
    """{worker_id: WorkerDeviceUse} con el ultimo movil que cogio cada una.

    Devuelve la fila de uso entera, no solo el dispositivo, porque sin la fecha
    el dato enganaria: los telefonos no son de nadie, cada una coge el que esta
    libre, asi que "su movil" caduca en horas. La pantalla tiene que poder decir
    "hace 5 min" o "ayer".

    Una sola consulta con el dispositivo ya cargado: la columna de Empleados lo
    pinta en bucle y sin esto serian tantas consultas como empleados.
    """
    filas = (WorkerDeviceUse.query
             .options(joinedload(WorkerDeviceUse.device))
             .order_by(WorkerDeviceUse.last_used.asc())
             .all())
    # Ascendente y sobrescribiendo: la ultima que se escribe es la mas reciente.
    return {f.worker_id: f for f in filas if f.device is not None}


def _movil_de_cada_conectada() -> dict:
    """{worker_id: WorkerDevice} de quien esta usando un movil ahora mismo.

    Para el panel principal: con los telefonos rotando, saber quien esta
    conectada sin saber que aparato lleva no sirve para ir a buscarlo.

    Solo cuenta lo reciente (`WorkerDevice.MINUTOS_EN_USO`). Un movil que se dejo
    en la mesa hace dos horas no lo lleva nadie, por mucho que la ultima que lo
    toco siga conectada desde otro.
    """
    corte = datetime.now() - timedelta(minutes=WorkerDevice.MINUTOS_EN_USO)
    filas = (WorkerDeviceUse.query
             .options(joinedload(WorkerDeviceUse.device))
             .filter(WorkerDeviceUse.last_used >= corte)
             .order_by(WorkerDeviceUse.last_used.asc())
             .all())
    return {f.worker_id: f.device for f in filas if f.device is not None}


def desde_hace(cuando) -> str:
    """"hace 5 min", "ayer", "el 03/09". En castellano y sin precision de mas.

    Con los moviles rotando, una fecha suelta no dice lo que hace falta saber:
    lo que importa es si el dato es de ahora o de la semana pasada, y eso una
    marca de tiempo exacta obliga a calcularlo mentalmente cada vez.
    """
    if not cuando:
        return 'nunca'
    seg = (datetime.now() - cuando).total_seconds()
    if seg < 60:
        return 'ahora mismo'
    if seg < 3600:
        return 'hace %d min' % (seg // 60)
    if seg < 86400:
        horas = int(seg // 3600)
        return 'hace %d hora%s' % (horas, 's' if horas != 1 else '')
    dias = int(seg // 86400)
    if dias == 1:
        return 'ayer'
    if dias < 7:
        return 'hace %d dias' % dias
    return 'el %s' % cuando.strftime('%d/%m')


def _dispositivo_actual() -> int | None:
    """El id del movil desde el que llega esta peticion, para sellar el registro.

    None si no manda la cabecera (la app Android antigua) o si el movil todavia
    no esta dado de alta, que solo puede pasar en la primera peticion de todas.
    Nunca levanta: sellar el aparato no puede impedir guardar una limpieza.
    """
    try:
        uid = _device_uid_valido(request.headers.get('X-Device-Id'))
        if not uid:
            return None
        fila = db.session.query(WorkerDevice.id).filter_by(device_uid=uid).first()
        return fila[0] if fila else None
    except Exception as e:
        app.logger.warning('No se pudo resolver el dispositivo: %s', e)
        return None


# ── Traduccion del contenido que escribe coordinacion ────────────────────────

# Que campos se traducen de cada modelo. Deliberadamente corto: se traduce lo
# que una trabajadora necesita entender para hacer bien su trabajo, no todo lo
# que hay escrito. Un numero de habitacion o el nombre de un grupo ("Planta 2")
# no gana nada traducido, y cada texto traducido es uno mas que mantener.
CAMPOS_TRADUCIBLES = {
    'care_type': ('name', 'instructions'),
    'checklist_item': ('text',),
    'resident': ('relevant_info', 'allergies'),
}


def _huella(texto: str) -> str:
    """Resumen del original, para saber si una traduccion se ha quedado vieja."""
    import hashlib
    return hashlib.md5((texto or '').strip().encode('utf-8')).hexdigest()


def _traducciones_de(entity_type: str, ids, lang: str) -> dict:
    """Todas las traducciones vigentes de esas entidades, en una sola consulta.

    Devuelve {(entity_id, field): texto}. Se consulta en bloque porque el uso
    real es una lista —los tipos de atencion de una pantalla, los items del
    checklist de una zona— y una consulta por fila seria un N+1 en el camino
    critico de la webapp.
    """
    if lang == 'es' or not ids:
        return {}
    filas = ContentTranslation.query.filter(
        ContentTranslation.entity_type == entity_type,
        ContentTranslation.entity_id.in_(list(ids)),
        ContentTranslation.lang == lang,
    ).all()
    return {(f.entity_id, f.field): f for f in filas}


def _aplicar_traduccion(fila, original: str) -> str:
    """El texto traducido, o el original si no hay o si se quedo desfasado.

    Desfasada es la que se hizo sobre un castellano que despues cambio. Vale
    mas ensenar el original que una instruccion que ya no dice lo mismo.
    """
    if not fila or not original:
        return original
    if fila.source_hash != _huella(original):
        return original
    return fila.text


def _texto_traducido(entity_type: str, entity_id: int, field: str,
                     original: str, lang: str) -> str:
    """Un solo texto traducido. Para listas, usar `_traducciones_de`."""
    if lang == 'es' or not original:
        return original
    fila = ContentTranslation.query.filter_by(
        entity_type=entity_type, entity_id=entity_id,
        field=field, lang=lang).first()
    return _aplicar_traduccion(fila, original)


def _idioma_peticion() -> str:
    """El idioma de quien hace la peticion, segun su ficha.

    Sale del token y no de una cabecera o un parametro: el idioma es una
    propiedad de la persona, no de la peticion, y asi nadie puede pedir el
    contenido en un idioma que no es el suyo por error.
    """
    try:
        username = get_jwt_identity()
    except RuntimeError:
        return 'es'
    if not username:
        return 'es'
    worker = Cleaner.query.filter_by(username=username).first()
    return _idioma_valido(worker.lang if worker else None)


_TRADUCCIONES_CACHE: dict | None = None


def _traducciones_webapp() -> dict:
    """La tabla de traducciones de la webapp, leida una sola vez.

    Va incrustada en el HTML de /worker y no servida desde /static porque el
    service worker guarda /static con estrategia cache primero: alli habria que
    versionar el fichero en cada cambio, y aqui se actualiza en el mismo
    despliegue que el resto de la pagina.
    """
    global _TRADUCCIONES_CACHE
    if _TRADUCCIONES_CACHE is None:
        import json as _json
        import os as _os
        ruta = _os.path.join(_os.path.dirname(__file__), 'i18n', 'worker.json')
        try:
            with open(ruta, encoding='utf-8') as fh:
                _TRADUCCIONES_CACHE = _json.load(fh)
        except (OSError, ValueError) as e:
            # Sin traducciones la webapp funciona en castellano. Es peor dejar a
            # nadie sin poder trabajar que dejar a alguien sin su idioma.
            app.logger.error('No se han podido leer las traducciones: %s', e)
            _TRADUCCIONES_CACHE = {}
    return _TRADUCCIONES_CACHE


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

def _checklist_de_zona(room) -> list:
    """Items activos del checklist que tocan al tipo de zona de esta habitacion.

    Un item sin ninguna zona marcada no sale en ninguna limpieza: son los
    heredados de cuando el checklist era una lista unica y estan a la espera de
    que coordinacion les marque las suyas desde el panel. Sacarlos en todas
    seria adivinar, y apareceria donde no toca.

    `any()` se traduce a un EXISTS, que funciona igual en SQLite y PostgreSQL.
    """
    if not room or not room.room_type_id:
        return []
    return (ChecklistItem.query
            .filter(ChecklistItem.active.is_(True),
                    ChecklistItem.room_types.any(RoomType.id == room.room_type_id))
            .order_by(ChecklistItem.sort_order, ChecklistItem.id)
            .all())


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


def formato_constante(valor) -> str:
    """El valor de una constante como se ensena: 120, no 120.0; 36.5, si.

    Una tension de "160.0/95.0" se lee peor que "160/95", y el decimal de un
    peso si hace falta. Lo usan la lista de registros, la ficha del residente y
    las graficas, que tienen que decir lo mismo.
    """
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return ''
    return str(int(numero)) if numero.is_integer() else f'{numero:.1f}'


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
