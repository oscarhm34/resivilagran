from __future__ import annotations
from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import JWTManager
from flask_login import LoginManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from .config import Config

app = Flask(__name__)
app.config.from_object(Config)
app.config['JWT_SECRET_KEY'] = Config.JWT_SECRET_KEY

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Enforce foreign keys in SQLite
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine


@sa_event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    import sqlite3
    if isinstance(dbapi_conn, sqlite3.Connection):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
jwt = JWTManager(app)
csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")


app.config['WTF_CSRF_TIME_LIMIT'] = 7200  # 2 hour token validity

login_manager = LoginManager(app)
login_manager.login_view = 'admin_bp.admin_login'
login_manager.login_message = 'Debes iniciar sesión para acceder a esta página.'
login_manager.login_message_category = 'warning'

from .models import Cleaner  # noqa: E402


@login_manager.user_loader
def load_user(user_id: str) -> Cleaner | None:
    return Cleaner.query.get(int(user_id))


# ── Track last_active for workers (JWT) and admins (Flask-Login) ───────────
from datetime import datetime as _dt, timedelta as _td

_ACTIVE_THROTTLE = _td(seconds=60)


def _update_if_stale(cleaner):
    """Update last_active only if more than 60s since last update."""
    now = _dt.now()
    if cleaner.last_active and (now - cleaner.last_active) < _ACTIVE_THROTTLE:
        return
    cleaner.last_active = now
    db.session.commit()


@app.after_request
def _track_last_active(response):
    try:
        from flask_login import current_user as _cu
        if _cu and getattr(_cu, 'is_authenticated', False):
            _update_if_stale(_cu)
            return response
    except Exception:
        pass
    try:
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
        if identity:
            cleaner = Cleaner.query.filter_by(username=identity).first()
            if cleaner:
                _update_if_stale(cleaner)
    except Exception:
        pass
    return response


from . import routes, models  # noqa: E402, F401
from .blueprints.nfc import bp as nfc_bp  # noqa: E402
from .blueprints.training import bp as training_bp  # noqa: E402
from .blueprints.documents import bp as documents_bp  # noqa: E402
from .blueprints.chat import bp as chat_bp  # noqa: E402
from .blueprints.shifts import bp as shifts_bp  # noqa: E402
from .blueprints.cleaning import bp as cleaning_bp  # noqa: E402
from .blueprints.residents import bp as residents_bp  # noqa: E402
from .blueprints.admin import bp as admin_bp  # noqa: E402
from .blueprints.care import bp as care_bp  # noqa: E402
from .blueprints.incidents import bp as incidents_bp  # noqa: E402
from .blueprints.notifications import bp as notifications_bp  # noqa: E402
from .blueprints.assessments import bp as assessments_bp  # noqa: E402
from .blueprints.medication import bp as medication_bp  # noqa: E402
from .blueprints.activities import bp as activities_bp  # noqa: E402
app.register_blueprint(nfc_bp)
app.register_blueprint(training_bp)
app.register_blueprint(documents_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(shifts_bp)
app.register_blueprint(cleaning_bp)
app.register_blueprint(residents_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(care_bp)
app.register_blueprint(incidents_bp)
app.register_blueprint(notifications_bp)
app.register_blueprint(assessments_bp)
app.register_blueprint(medication_bp)
app.register_blueprint(activities_bp)

# Exempt all blueprints from CSRF — app is internal (local network only)
# CSRF meta tag + JS auto-injection in base.html provides protection for admin forms
# API routes use JWT Bearer tokens instead of CSRF
for _bp in [nfc_bp, chat_bp, notifications_bp, admin_bp, training_bp, documents_bp,
            shifts_bp, cleaning_bp, residents_bp, care_bp, incidents_bp,
            assessments_bp, medication_bp, activities_bp]:
    csrf.exempt(_bp)


@app.errorhandler(404)
def not_found(e: Exception) -> tuple:
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'error': 'No encontrado'}), 404
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(e: Exception) -> tuple:
    db.session.rollback()
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'error': 'Error interno del servidor'}), 500
    return render_template('500.html'), 500
