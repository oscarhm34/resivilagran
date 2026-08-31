import os
from dotenv import load_dotenv

load_dotenv()


def _persistent_secret(env_var, fallback_file):
    """Return secret from env, or generate once and persist to file."""
    val = os.environ.get(env_var)
    if val:
        return val
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'instance', fallback_file)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    val = os.urandom(32).hex()
    with open(path, 'w') as f:
        f.write(val)
    return val


def _vapid_keys():
    """Return VAPID key pair, generating once and persisting to files."""
    instance_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'instance')
    priv_path = os.path.join(instance_dir, '.vapid_private_key')
    pub_path = os.path.join(instance_dir, '.vapid_public_key')
    if os.path.exists(priv_path) and os.path.exists(pub_path):
        with open(priv_path) as f:
            priv = f.read().strip()
        with open(pub_path) as f:
            pub = f.read().strip()
        return priv, pub
    os.makedirs(instance_dir, exist_ok=True)
    try:
        from py_vapid import Vapid
        vapid = Vapid()
        vapid.generate_keys()
        priv = vapid.private_pem().decode('utf-8') if isinstance(vapid.private_pem(), bytes) else vapid.private_pem()
        pub = vapid.public_key_urlsafe_base64()
        with open(priv_path, 'w') as f:
            f.write(priv)
        with open(pub_path, 'w') as f:
            f.write(pub)
        return priv, pub
    except Exception:
        return None, None


_VAPID_PRIVATE, _VAPID_PUBLIC = _vapid_keys()


def _database_uri() -> str:
    """URI de la base de datos, con red de seguridad para produccion.

    Sin esta guarda, un DATABASE_URL ausente (un typo en el compose, un .env mal
    montado) no fallaba: la app arrancaba contra un SQLite vacio dentro del
    contenedor, `db.create_all()` le creaba el esquema y todo parecia funcionar
    mientras se escribian atenciones y medicacion en una base que se pierde al
    recrear el contenedor.

    El fallback a SQLite se conserva en local y en los tests, que dependen de el.
    Solo se exige DATABASE_URL dentro de un contenedor, que es donde corre
    produccion (/.dockerenv lo crea Docker).
    """
    url = os.environ.get('DATABASE_URL')
    if url:
        return url
    if os.path.exists('/.dockerenv'):
        raise RuntimeError(
            'Falta DATABASE_URL. La aplicacion no arranca contra SQLite dentro '
            'del contenedor: los datos se perderian al recrearlo. Revisa '
            'DB_PASSWORD y DATABASE_URL en el .env del servidor.')
    return 'sqlite:///cleaning_service.db'


class Config:
    SECRET_KEY = _persistent_secret('SECRET_KEY', '.secret_key')
    JWT_SECRET_KEY = _persistent_secret('JWT_SECRET_KEY', '.jwt_secret_key')
    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY') or _VAPID_PRIVATE
    VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY') or _VAPID_PUBLIC
    VAPID_CLAIMS_EMAIL = os.environ.get('VAPID_CLAIMS_EMAIL', 'mailto:admin@lavilagran.com')
