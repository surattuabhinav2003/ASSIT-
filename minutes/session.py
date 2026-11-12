"""One recording: starts the capture, keeps what is heard (saved every few seconds, so a crash loses little),
reads the roster from Teams, and when stopped runs the after-meeting pass in the background."""
import threading
import time
import traceback

from . import config, finish, names, store
from .capture import Capture


class Session:
    def __init__(self, client, you_name, log=print):
        self.client, self.you_name, self.log = client, you_name, log
        self.mid = None
        self.capture = None
        self.roster = None
        self.t0 = None
        self.recording = False
        self.rows = []                      # finished lines: {id, side, speaker, t0, t1, text}
        self.partial = {"them": "", "you": ""}
        self.levels = {"them": 0.0, "you": 0.0}
        self.notices = []
        self.status = ""                    # what the after-meeting pass is doing
        self._spans, self._texts = {}, {}
        self._lock = threading.RLock()
        self._saved = 0
        self._stop_evt = threading.Event()

    # ---------------------------------------------------------------- recording
    def start(self, title, app, keep_audio=True):
        meta = store.create(title, app, self.you_name)
        self.mid = meta["id"]
        self.capture = Capture(self.client, self._event, on_level=self._level)
        self.capture.start(store.track_path(self.mid, "others") if keep_audio else None,
                           store.track_path(self.mid, "you") if keep_audio else None)
        self.t0 = self.capture.t0
        if app == "Teams":
            self.roster = names.TeamsRoster(self.t0, self.you_name)
        self.recording = True
        threading.Thread(target=self._autosave, daemon=True, name="autosave").start()
        self.log(f"[session] recording {self.mid}: {self.capture.devices()}")
        return self.mid

    def _level(self, side, v):
        self.levels[side] = v

    def _event(self, kind, side, line, text, span):
        with self._lock:
            key = (side, line)
            if kind == "text":
                self.partial[side] = text or ""
            elif kind == "start":
                self.partial[side] = ""
            elif kind == "end":
                self._spans[key] = span
                self._try_commit(key)
            elif kind == "final":
                self._texts[key] = (text or "").strip()
                self.partial[side] = ""
                self._try_commit(key)
            elif kind == "drop":
                self.partial[side] = ""
                self._spans.pop(key, None)
                self._texts.pop(key, None)
            elif kind == "error":
                if text and text not in self.notices:
                    self.notices.append(text)
                    del self.notices[:-3]

    def _try_commit(self, key):
        if key in self._spans and key in self._texts:
            side = key[0]
            t0, t1 = self._spans.pop(key)
            text = self._texts.pop(key)
            if text:
                self.rows.append({"id": len(self.rows) + 1, "side": side, "speaker": self._speaker(side),
                                  "t0": round(t0, 2), "t1": round(t1, 2), "text": text})
                self.rows.sort(key=lambda r: r["t0"])

    def _speaker(self, side):
        if side == "you":
            return self.you_name
        r = self.roster.snapshot() if self.roster else None
        others = [n for n in (r or {}).get("names", []) if n != self.you_name]
        return others[0] if len(others) == 1 else "Others"

    def _autosave(self):
        while not self._stop_evt.wait(5):
            self.save()

    def save(self):
        with self._lock:
            if self.mid:
                store.save_lines(self.mid, list(self.rows))

    def live(self):
        with self._lock:
            rows = list(self.rows[-400:])
        r = self.roster.snapshot() if self.roster else None
        notices = list(self.notices)
        elapsed = time.time() - self.t0 if self.t0 else 0
        if self.recording and elapsed > 45 and self.capture and self.capture.max_level["them"] < 0.01:
            notices.append("No sound from the other side yet. If you are in a meeting, check that it plays through the Windows "
                           "default speaker (Settings, Check audio).")
        return {"recording": self.recording, "id": self.mid, "elapsed": round(time.time() - self.t0, 1) if self.t0 and self.recording else 0,
                "rows": rows, "partial": dict(self.partial), "levels": dict(self.levels), "notices": notices,
                "roster": (r or {}).get("names", []), "roster_ok": bool(r and r["ok"]),
                "mic_stale": bool(self.capture and self.capture.stale()), "status": self.status}

    # ---------------------------------------------------------------- stopping
    def stop(self):
        """Stops hearing and starts the after-meeting pass. Returns the meeting id at once."""
        if not self.recording:
            return self.mid
        self.recording = False
        self._stop_evt.set()
        roster = self.roster.snapshot() if self.roster else None
        if self.roster:
            self.roster.stop()
        try:
            self.capture.stop()
        except Exception:
            self.log("[session] stopping the capture: " + traceback.format_exc())
        time.sleep(0.8)                       # the last words arrive just after the voice stops
        with self._lock:
            for key in list(self._texts):     # a line whose end never came: keep its words
                side = key[0]
                self.rows.append({"id": len(self.rows) + 1, "side": side, "speaker": self._speaker(side),
                                  "t0": round(time.time() - self.t0 - 3, 2), "t1": round(time.time() - self.t0, 2), "text": self._texts.pop(key)})
            self.save()
        store.update(self.mid, ended=time.time(), status="processing", roster=roster)
        threading.Thread(target=self._finish, args=(roster,), daemon=True, name="finish").start()
        return self.mid

    def _finish(self, roster):
        try:
            finish.run(self.client, self.mid, roster=roster, you_name=self.you_name,
                       status=lambda s: setattr(self, "status", s), log=self.log)
        except Exception as e:
            self.log("[session] after-meeting pass failed: " + traceback.format_exc())
            store.update(self.mid, status="error", error=f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            self.status = ""
