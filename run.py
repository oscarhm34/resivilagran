from app import app, db
from datetime import datetime


@app.context_processor
def inject_now():
    return {'now': datetime.now()}


with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5001, use_reloader=False)
