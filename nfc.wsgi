import sys
import os

activate_this = '/volume1/web/NFC2/venv/bin/activate_this.py'
with open(activate_this) as file_:
    exec(file_.read(), dict(__file__=activate_this))

sys.path.insert(0, '/volume1/web/NFC2')

os.environ['FLASK_APP'] = 'run.py'
os.environ['PYTHONIOENCODING'] = 'utf-8'

from run import app as application  # noqa: E402
