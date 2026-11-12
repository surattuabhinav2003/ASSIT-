"""Keys, model names and the saved settings. Nothing here is printed or logged."""
import json
import os

from dotenv import load_dotenv

from . import paths

paths.ensure()
# Per-laptop file first (what the installer writes), then a .env beside the code (a developer's).
load_dotenv(paths.CONFIG_ENV)
load_dotenv(os.path.join(paths.CODE, ".env"))

LIVE_MODEL = os.getenv("MINUTES_LIVE_MODEL", "gpt-live-transcribe")           # words while people talk
LIVE_MODEL_TELUGU = os.getenv("MINUTES_LIVE_MODEL_TELUGU", "gpt-4o-transcribe")   # live words for Telugu and mixed speech (measured: clean Telugu script)
DIARIZE_MODEL = os.getenv("MINUTES_DIARIZE_MODEL", "gpt-4o-transcribe-diarize")  # who said what, after the meeting
MOM_MODEL = os.getenv("MINUTES_MOM_MODEL", "gpt-6-sol")                       # names clean-up and the minutes

# what is spoken in the meeting: the live transcription and the after-meeting pass are told
LANGUAGES = {"en": "English", "te": "Telugu", "mixed": "Telugu and English mixed"}

DEFAULTS = {
    "language": "en",            # en | te | mixed (people switch between Telugu and English)
    "minutes_language": "english",   # english: minutes always in English | meeting: in the language spoken
    "auto_start": True,          # start recording by itself when a Teams meeting starts
    "keep_audio": True,          # keep the two audio tracks on this laptop (the transcript is always kept)
    "audio_days": 30,            # audio older than this is deleted; the transcript and minutes stay
    "your_name": "",             # how you appear in transcripts; empty = the Windows account's name
}


def api_key():
    """The OpenAI key: the environment (a developer's .env) first, else the one saved on this laptop."""
    if os.getenv("OPENAI_API_KEY"):
        return os.getenv("OPENAI_API_KEY")
    from . import secrets
    return secrets.load_key()


def load_settings():
    try:
        with open(paths.SETTINGS, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(values):
    cur = load_settings()
    cur.update({k: v for k, v in values.items() if k in DEFAULTS})
    tmp = paths.SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=1)
    os.replace(tmp, paths.SETTINGS)
    return cur
