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


class Config:
    SECRET_KEY = _persistent_secret('SECRET_KEY', '.secret_key')
    JWT_SECRET_KEY = _persistent_secret('JWT_SECRET_KEY', '.jwt_secret_key')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///cleaning_service.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
