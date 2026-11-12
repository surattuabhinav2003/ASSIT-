"""CF Minutes: the window, the tray icon, and the watcher that notices meetings."""
import ctypes
import http.server
import json
import os
import re
import secrets as _secrets
import shutil
import sys
import threading
import time
import traceback

import webview

from . import __version__, audiotest, config, export, finish, names, paths, store, teams, updater
from . import secrets as keystore
from .session import Session

TITLE = "CF Minutes"
# apps that, using the microphone, probably mean a meeting. Our own process is pythonw.exe: never offered.
MEETING_APPS = {"zoom.exe": "Zoom", "chrome.exe": "a browser meeting (Meet, Zoom, Webex...)", "msedge.exe": "a browser meeting",
                "slack.exe": "Slack", "webex.exe": "Webex", "ciscocollabhost.exe": "Webex", "discord.exe": "Discord",
                "skype.exe": "Skype", "googlemeet": "Google Meet", "whatsapp.exe": "WhatsApp"}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- recordings played in the window
class AudioServer:
    """Serves the meeting audio to the window on this laptop only (127.0.0.1, a random port, a secret in the
    address), with Range support so the player can jump around."""

    def __init__(self):
        self.token = _secrets.token_urlsafe(18)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                m = re.match(rf"^/a/{re.escape(outer.token)}/([\w\-]+)/(you|others)\.wav$", self.path.split("?")[0])
                if not m:
                    self.send_error(404)
                    return
                path = store.track_path(m.group(1), m.group(2))
                if not os.path.isfile(path):
                    self.send_error(404)
                    return
                size = os.path.getsize(path)
                start, end = 0, size - 1
                rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
                if rng:
                    if rng.group(1):
                        start = int(rng.group(1))
                    if rng.group(2):
                        end = min(size - 1, int(rng.group(2)))
                    elif not rng.group(1):
                        start = 0
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = f.read(min(65536, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="audio-server").start()

    def url(self, mid, which):
        return f"http://127.0.0.1:{self.port}/a/{self.token}/{mid}/{which}.wav"


# ---------------------------------------------------------------- what the window can ask for
class Api:
    def __init__(self, app):
        self._app = app

    # state
    def state(self):
        return self._app.state()

    def live(self):
        return self._app.live()

    # recording
    def start(self, title="", language=""):
        return self._app.start_recording(title, "Manual", manual=True, language=language)

    def open_actions(self):
        """Action items from the latest meetings, newest first, for the Home page."""
        out = []
        for m in store.listing()[:12]:
            for a in (store.mom(m["id"]) or {}).get("actions", []):
                out.append({"meeting": m["id"], "title": m.get("title") or "Meeting", "started": m.get("started"),
                            "owner": a.get("owner") or "", "task": a.get("task") or "", "due": a.get("due") or ""})
        return out[:12]

    def stop(self):
        return self._app.stop_recording()

    def dismiss_suggestion(self):
        self._app.suggestion = None
        return True

    # library
    def meetings(self):
        return store.listing()

    def meeting(self, mid):
        meta = store.get(mid)
        if not meta:
            return None
        a = self._app.audio
        return {"meta": meta, "lines": store.lines(mid), "mom": store.mom(mid),
                "audio": {w: a.url(mid, w) for w in ("you", "others") if meta.get(f"has_{w}_audio")}}

    def rename_speaker(self, mid, old, new):
        return store.rename_speaker(mid, old, new)

    def set_title(self, mid, title):
        title = (title or "").strip()[:120]
        if title:
            store.update(mid, title=title)
        return True

    def save_mom(self, mid, mom):
        cur = store.mom(mid) or {}
        for k in ("summary", "decisions", "open_questions"):
            if isinstance(mom.get(k), list):
                cur[k] = [str(x).strip() for x in mom[k] if str(x).strip()]
        if isinstance(mom.get("actions"), list):
            cur["actions"] = [{"owner": str(a.get("owner") or "Unassigned"), "task": str(a.get("task") or "").strip(),
                               "due": str(a.get("due") or "")} for a in mom["actions"] if str(a.get("task") or "").strip()]
        cur["edited"] = time.time()
        store.save_mom(mid, cur)
        return True

    def regenerate(self, mid):
        return self._app.reprocess(mid, minutes_only=True)

    def reprocess(self, mid):
        return self._app.reprocess(mid)

    def delete(self, mid):
        if self._app.session and self._app.session.mid == mid and self._app.session.recording:
            return False
        store.delete(mid)
        return True

    def delete_audio(self, mid):
        store.delete_audio(mid)
        return True

    def open_folder(self, mid=""):
        path = os.path.join(paths.MEETINGS, mid) if mid else paths.HOME
        if os.path.isdir(path):
            os.startfile(path)
        return True

    def copy_text(self, mid, kind="minutes"):
        return export.transcript_text(mid) if kind == "transcript" else export.minutes_markdown(mid)

    def export(self, mid, kind):
        """Asks where to save, then writes Word (kind "docx") or Markdown ("md"). Returns the path or ""."""
        meta = store.get(mid)
        safe = re.sub(r'[\\/:*?"<>|]+', "", meta.get("title") or "Meeting").strip()[:60] or "Meeting"
        day = time.strftime("%Y-%m-%d", time.localtime(meta.get("started") or time.time()))
        w = self._app.window
        res = w.create_file_dialog(webview.SAVE_DIALOG, save_filename=f"{safe} {day} minutes.{kind}",
                                   file_types=("Word document (*.docx)",) if kind == "docx" else ("Markdown (*.md)",))
        if not res:
            return ""
        path = res if isinstance(res, str) else res[0]
        if kind == "docx":
            export.minutes_docx(mid, path, with_transcript=True)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(export.minutes_markdown(mid, with_transcript=True))
        return path

    # settings and key
    def settings(self):
        return config.load_settings()

    def set_settings(self, values):
        out = config.save_settings(values or {})
        self._app.settings_changed()
        return out

    def set_key(self, key):
        key = (key or "").strip()
        if not re.match(r"^sk-[\w\-]{20,}$", key):
            return {"ok": False, "error": "That does not look like an OpenAI key (it starts with sk-)."}
        try:
            from openai import OpenAI
            OpenAI(api_key=key).models.list()
        except Exception as e:
            return {"ok": False, "error": f"OpenAI did not accept this key ({type(e).__name__})."}
        keystore.save_key(key)
        self._app.client = None
        self._app.resume_unfinished()
        return {"ok": True}

    def check_audio(self):
        """Plays a short tone to the default speaker and listens to it through the same capture the meetings use
        (loopback), and listens to the microphone. Says which side is not working."""
        if self.session and self.session.recording:
            return {"ok": False, "text": "Stop the recording first."}
        return audiotest.run()

    def forget_key(self):
        keystore.forget_key()
        self._app.client = None
        return True


# ---------------------------------------------------------------- the app
class App:
    def __init__(self):
        self.settings = config.load_settings()
        self.client = None
        self.session = None
        self.window = None
        self.tray = None
        self.suggestion = None
        self.audio = AudioServer()
        self.api = Api(self)
        self._auto_started = False
        self._manual_stop_during_call = False
        self._finishing = set()
        self._quit = threading.Event()
        self.updates = None

    # ---- helpers
    def you_name(self):
        return (self.settings.get("your_name") or names.display_name() or os.environ.get("USERNAME") or "You").strip()

    def get_client(self):
        if self.client is None:
            key = config.api_key()
            if not key:
                return None
            from openai import OpenAI
            self.client = OpenAI(api_key=key, timeout=120, max_retries=2)
        return self.client

    def _idle(self):
        """True when nothing would be lost by restarting: no recording, nothing being written."""
        return not (self.session and self.session.recording) and not self._finishing and not any(
            m.get("status") in ("recording", "processing") for m in store.listing())

    def settings_changed(self):
        self.settings = config.load_settings()

    def state(self):
        rec = bool(self.session and self.session.recording)
        busy = [m["id"] for m in store.listing() if m.get("status") == "processing"]
        return {"version": __version__, "key": bool(config.api_key()), "recording": rec, "auto": self.settings["auto_start"], "language": self.settings.get("language", "en"),
                "languages": config.LANGUAGES, "elapsed": round(time.time() - self.session.t0) if self.session and self.session.recording else 0,
                "you": self.you_name(), "suggestion": self.suggestion, "processing": busy, "home": paths.HOME,
                "meeting_id": self.session.mid if self.session else None,
                "update": getattr(getattr(self, "updates", None), "state", {"error": "", "version": ""})}

    def live(self):
        s = self.session
        if not s:
            return {"recording": False}
        return s.live()

    # ---- recording
    def start_recording(self, title, app_name, manual=False, language=""):
        if self.session and self.session.recording:
            return {"ok": False, "error": "Already recording."}
        client = self.get_client()
        if client is None:
            return {"ok": False, "error": "Add your OpenAI key first (Settings)."}
        try:
            self.session = Session(client, self.you_name(), log=log)
            lang = language if language in config.LANGUAGES else self.settings.get("language", "en")
            mid = self.session.start(title, app_name, keep_audio=True, language=lang)
        except Exception as e:
            log("[app] could not start: " + traceback.format_exc())
            self.session = None
            return {"ok": False, "error": str(e) if isinstance(e, RuntimeError) else f"Could not start recording ({type(e).__name__}). Try Settings, Check audio."}
        self._auto_started = not manual
        self.suggestion = None
        self._tray_state()
        self.notify("Recording started", "Everyone on the call should know it is being recorded.")
        return {"ok": True, "id": mid}

    def stop_recording(self):
        if not (self.session and self.session.recording):
            return {"ok": False}
        if teams.in_call():
            self._manual_stop_during_call = True            # do not start again for this same call
        mid = self.session.stop()
        self._tray_state()
        return {"ok": True, "id": mid}

    def reprocess(self, mid, minutes_only=False):
        client = self.get_client()
        if client is None:
            return {"ok": False, "error": "Add your OpenAI key first (Settings)."}
        if mid in self._finishing:
            return {"ok": True}
        self._finishing.add(mid)
        store.update(mid, status="processing", error="")

        def work():
            try:
                meta = store.get(mid)
                if minutes_only:
                    rows = store.lines(mid)
                    store.save_mom(mid, finish.make_minutes(client, meta, rows, log))
                    store.update(mid, status="ready")
                else:
                    finish.run(client, mid, roster=meta.get("roster"), you_name=meta.get("you") or self.you_name(), log=log)
            except Exception as e:
                log("[app] reprocess failed: " + traceback.format_exc())
                store.update(mid, status="error", error=f"{type(e).__name__}: {str(e)[:200]}")
            finally:
                self._finishing.discard(mid)
        threading.Thread(target=work, daemon=True, name="reprocess").start()
        return {"ok": True}

    def resume_unfinished(self):
        """A meeting left 'recording' (the app or laptop stopped) or 'processing' (closed while the pass ran) is
        finished from what was saved."""
        for m in store.listing():
            if self.session and m["id"] == self.session.mid:
                continue
            if m.get("status") in ("recording", "processing") and m["id"] not in self._finishing:
                if m.get("status") == "recording":
                    store.update(m["id"], ended=m.get("ended") or time.time())
                log(f"[app] finishing an unfinished meeting {m['id']}")
                self.reprocess(m["id"])

    # ---- meetings noticed on this laptop
    def watch(self):
        in_call_for, out_for = 0, 0
        offered = set()
        last_clean = 0
        while not self._quit.wait(2.0):
            try:
                rec = bool(self.session and self.session.recording)
                call = teams.in_call()
                if call:
                    in_call_for, out_for = in_call_for + 1, 0
                else:
                    out_for += 1
                    in_call_for = 0
                    if out_for >= 3:
                        self._manual_stop_during_call = False
                if not rec and self.settings["auto_start"] and call and in_call_for >= 3 and not self._manual_stop_during_call \
                        and config.api_key():
                    title = re.sub(r"\s*\|\s*Microsoft Teams\s*$", "", teams.meeting_title() or "", flags=re.I).strip()
                    self.start_recording(title or "Teams meeting", "Teams")
                elif rec and self._auto_started and not call and not teams.meeting_open() and out_for >= 12:
                    self.stop_recording()                          # the call ended (about 25 s with no Teams call)
                elif not rec and not call:
                    using = [a.lower() for a in teams.apps_using_mic()]
                    hit = next((a for a in using if a in MEETING_APPS), None)
                    if hit and hit not in offered:
                        offered.add(hit)
                        self.suggestion = {"app": MEETING_APPS[hit], "key": hit}
                        self.notify("Is this a meeting?", f"{MEETING_APPS[hit]} is using your microphone. Open CF Minutes to record it.")
                    elif not hit:
                        offered.clear()
                        if self.suggestion:
                            self.suggestion = None
                if time.time() - last_clean > 3600:
                    last_clean = time.time()
                    store.forget_old_audio(self.settings["audio_days"])
            except Exception:
                log("[watch] " + traceback.format_exc())

    # ---- tray
    def _icon(self, recording):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(1, 41, 172, 255))
        d.rectangle((17, 22, 47, 26), fill=(255, 255, 255, 255))
        d.rectangle((17, 31, 40, 35), fill=(255, 255, 255, 255))
        d.rectangle((17, 40, 33, 44), fill=(255, 255, 255, 255))
        if recording:
            d.ellipse((38, 38, 62, 62), fill=(214, 40, 40, 255), outline=(255, 255, 255, 255), width=3)
        return img

    def start_tray(self):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("Open CF Minutes", lambda: self.show(), default=True),
            pystray.MenuItem(lambda item: "Stop recording" if self.session and self.session.recording else "Start recording",
                             lambda: self.stop_recording() if self.session and self.session.recording else self.start_recording("", "Manual", manual=True)),
            pystray.MenuItem("Quit", lambda: self.quit()))
        self.tray = pystray.Icon("cfminutes", self._icon(False), TITLE, menu)
        self.tray.run_detached()

    def _tray_state(self):
        if self.tray:
            rec = bool(self.session and self.session.recording)
            self.tray.icon = self._icon(rec)
            self.tray.title = TITLE + (" - recording" if rec else "")

    def notify(self, title, msg):
        try:
            if self.tray:
                self.tray.notify(msg, title)
        except Exception:
            pass

    # ---- window
    def show(self):
        try:
            self.window.show()
            self.window.restore()
        except Exception:
            pass

    def quit(self):
        self._quit.set()
        if self.session and self.session.recording:
            try:
                self.session.stop()
            except Exception:
                pass
            time.sleep(1.0)
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.window.destroy()
        os._exit(0)

    def run(self):
        paths.ensure()
        self.window = webview.create_window(
            TITLE, os.path.join(paths.UI, "index.html"), js_api=self.api, width=1200, height=800, min_size=(960, 620),
            background_color="#F4F7FB", text_select=True)

        def closing():
            # the X hides the window; recording goes on. Quit is in the tray menu.
            try:
                self.window.hide()
            except Exception:
                pass
            return False
        self.window.events.closing += closing

        def started():
            self.start_tray()
            threading.Thread(target=self.watch, daemon=True, name="watch").start()
            threading.Thread(target=self.resume_unfinished, daemon=True).start()
            self.updates = updater.Loop(idle=self._idle, quit_app=self.quit, log=log)
        webview.start(started, icon=os.path.join(paths.UI, "app.ico") if os.path.isfile(os.path.join(paths.UI, "app.ico")) else None)


_MUTEX = None


def _one_instance():
    """Only one copy listens. After an update the new copy waits (up to 15 s) for the old one to close."""
    global _MUTEX
    k32 = ctypes.windll.kernel32
    for _ in range(30 if "--restarted" in sys.argv else 1):
        _MUTEX = k32.CreateMutexW(None, False, "CFMinutes.single")
        if k32.GetLastError() != 183:
            return True
        k32.CloseHandle(_MUTEX)
        time.sleep(0.5)
    return False


def _log_to_file():
    if sys.executable.lower().endswith("pythonw.exe") or not sys.stdout or not sys.stdout.isatty():
        paths.ensure()
        f = open(paths.LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
        print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} started ({__version__})", flush=True)


def main():
    _log_to_file()
    if not _one_instance():
        print("Already running.")
        return
    App().run()
