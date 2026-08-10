from __future__ import annotations
from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import JWTManager
from flask_login import LoginManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from .config import Config

app = Flask(__name__)
app.config.from_object(Config)
app.config['JWT_SECRET_KEY'] = Config.JWT_SECRET_KEY

db = SQLAlchemy(app)
migrate = Migrate(app, db)
jwt = JWTManager(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'
login_manager.login_message = 'Debes iniciar sesión para acceder a esta página.'
login_manager.login_message_category = 'warning'

from .models import Cleaner  # noqa: E402


@login_manager.user_loader
def load_user(user_id: str) -> Cleaner | None:
    return Cleaner.query.get(int(user_id))


from . import routes, models  # noqa: E402, F401
from .blueprints.nfc import bp as nfc_bp  # noqa: E402
from .blueprints.training import bp as training_bp  # noqa: E402
from .blueprints.documents import bp as documents_bp  # noqa: E402
from .blueprints.chat import bp as chat_bp  # noqa: E402
from .blueprints.shifts import bp as shifts_bp  # noqa: E402
from .blueprints.cleaning import bp as cleaning_bp  # noqa: E402
app.register_blueprint(nfc_bp)
app.register_blueprint(training_bp)
app.register_blueprint(documents_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(shifts_bp)
app.register_blueprint(cleaning_bp)


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
