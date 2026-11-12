"""Where things live. Meetings are kept on this laptop only, in the Windows user's own profile."""
import os

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # the folder with run.py
HOME = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "CFMinutes")
MEETINGS = os.path.join(HOME, "meetings")
CONFIG_ENV = os.path.join(HOME, "config.env")
SETTINGS = os.path.join(HOME, "settings.json")
LOG = os.path.join(HOME, "logs", "app.log")
UI = os.path.join(CODE, "ui")
VOCAB = os.path.join(CODE, "knowledge", "vocab.json")


def ensure():
    for p in (HOME, MEETINGS, os.path.dirname(LOG)):
        os.makedirs(p, exist_ok=True)
