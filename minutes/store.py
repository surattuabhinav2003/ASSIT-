"""Meetings on this laptop. One folder per meeting under %LOCALAPPDATA%\CFMinutes\meetings\<id>\:

    meeting.json   title, times, status, who was there
    lines.json     the transcript: [{id, side, speaker, t0, t1, text}] (seconds from the start)
    mom.json       the minutes
    others.wav     what the laptop played (everyone else), 16 kHz mono  (optional, see settings)
    you.wav        the microphone, 16 kHz mono                          (optional)

Nothing here talks to a server or a database."""
import json
import os
import shutil
import threading
import time

from . import paths

_lock = threading.RLock()


def _dir(mid):
    if not mid or os.sep in mid or "/" in mid or mid.startswith("."):
        raise ValueError("bad meeting id")
    return os.path.join(paths.MEETINGS, mid)


def _read(mid, name, default):
    try:
        with open(os.path.join(_dir(mid), name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(mid, name, data):
    d = _dir(mid)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, name + ".tmp")
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, name))


def new_id():
    base = time.strftime("%Y%m%d-%H%M%S")
    mid, n = base, 1
    while os.path.exists(_dir(mid)):
        n += 1
        mid = f"{base}-{n}"
    return mid


def create(title, app, you, started=None):
    mid = new_id()
    meta = {"id": mid, "title": title or "Meeting", "app": app or "", "you": you, "started": started or time.time(),
            "ended": None, "status": "recording", "attendees": [], "error": ""}
    _write(mid, "meeting.json", meta)
    _write(mid, "lines.json", [])
    return meta


def get(mid):
    meta = _read(mid, "meeting.json", None)
    if not meta:
        return None
    meta["has_you_audio"] = os.path.isfile(os.path.join(_dir(mid), "you.wav"))
    meta["has_others_audio"] = os.path.isfile(os.path.join(_dir(mid), "others.wav"))
    return meta


def update(mid, **fields):
    with _lock:
        meta = _read(mid, "meeting.json", None)
        if meta is None:
            return None
        meta.update(fields)
        _write(mid, "meeting.json", meta)
        return meta


def lines(mid):
    return _read(mid, "lines.json", [])


def save_lines(mid, rows):
    _write(mid, "lines.json", rows)


def mom(mid):
    return _read(mid, "mom.json", None)


def save_mom(mid, data):
    _write(mid, "mom.json", data)


def track_path(mid, which):
    return os.path.join(_dir(mid), f"{which}.wav")


def listing():
    """Every meeting, newest first, with a one-line preview."""
    out = []
    try:
        ids = os.listdir(paths.MEETINGS)
    except OSError:
        return out
    for mid in ids:
        meta = get(mid)
        if not meta:
            continue
        rows = lines(mid)
        m = mom(mid) or {}
        meta["line_count"] = len(rows)
        meta["preview"] = (m.get("summary") or [""])[0] if isinstance(m.get("summary"), list) else (m.get("summary") or "")
        out.append(meta)
    out.sort(key=lambda x: x.get("started") or 0, reverse=True)
    return out


def rename_speaker(mid, old, new):
    """Every line said by `old` is now said by `new` (a name fixed once is fixed through the whole meeting)."""
    new = (new or "").strip()
    if not new or new == old:
        return 0
    with _lock:
        rows = lines(mid)
        n = 0
        for r in rows:
            if r.get("speaker") == old:
                r["speaker"], n = new, n + 1
        save_lines(mid, rows)
        meta = get(mid) or {}
        att = [new if a == old else a for a in meta.get("attendees", [])]
        update(mid, attendees=list(dict.fromkeys(att)))
        m = mom(mid)
        if m:
            m = json.loads(json.dumps(m).replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1]))
            save_mom(mid, m)
    return n


def delete(mid):
    shutil.rmtree(_dir(mid), ignore_errors=True)


def delete_audio(mid):
    for which in ("you", "others"):
        try:
            os.remove(track_path(mid, which))
        except OSError:
            pass


def forget_old_audio(days):
    """Deletes audio older than `days` (0 = never); transcripts and minutes stay."""
    if not days:
        return 0
    cutoff, n = time.time() - days * 86400, 0
    for meta in listing():
        if (meta.get("started") or 0) < cutoff and (meta.get("has_you_audio") or meta.get("has_others_audio")):
            delete_audio(meta["id"])
            n += 1
    return n
