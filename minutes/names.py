"""Who is in the meeting, from what Teams shows on screen (Windows UI Automation: the same tree a screen reader
reads). Nothing is installed into Teams and nothing leaves this laptop.

Each second the meeting window is read for participant tiles / roster rows: Teams labels them like
"Priya Shah, Unmuted, Video on". From those labels this keeps
  - the roster: every name seen in the meeting
  - a timeline: when someone was speaking, or the only person unmuted. That is evidence for finish.py
    when it decides which diarized voice is which person.
A person is never named from this alone: finish.py also uses the words spoken ("Thanks, Priya") and you
can rename anyone on screen.

The label format is Teams'; tools/dump_teams.py writes what Teams exposes during a real meeting, to tune the
patterns below if Teams changes."""
import ctypes
import re
import threading
import time

from . import teams

# words after the name in a tile's label
_STATE = re.compile(r"\b(un-?muted|muted|speaking|video (?:on|off)|video is (?:on|off)|camera (?:on|off)|context menu is available|"
                    r"presenting|sharing|raised hand|"
                    r"hand raised|pinned|spotlighted|organizer|presenter|attendee|host)\b", re.I)
_SAMPLE = re.compile(r"mute|speak|video|camera|present|host|organi[sz]er|attendee|hand|pinned|participant|joined|\(you\)", re.I)
_NOT_NAMES = {"mute", "unmute", "camera", "share", "react", "more", "leave", "chat", "people", "raise", "rooms",
              "apps", "view", "notes", "whiteboard", "captions", "record", "transcript", "back", "forward", "search",
              "settings and more", "calendar", "calls", "activity", "teams", "files", "meeting", "close", "minimize",
              "maximize", "restore"}
_PERSON = re.compile(r"^[^\W\d_][^\W\d_'.\-() ]*(?:[ '.\-]+[^\W\d_'.\-() ]+){0,4}\.?(?: \((?:You|Guest|External)\))?$", re.UNICODE)


def display_name():
    """The Windows account's display name ("Abhinav Surattu"), or ""."""
    try:
        buf = ctypes.create_unicode_buffer(256)
        size = ctypes.c_ulong(256)
        if ctypes.windll.secur32.GetUserNameExW(3, buf, ctypes.byref(size)):       # NameDisplay
            return buf.value.strip()
    except Exception:
        pass
    return ""


def parse_label(label):
    """"Priya Shah, Unmuted, Video on" -> {"name", "speaking", "unmuted", "muted", "you"}; None when it is not
    a participant label."""
    label = (label or "").strip()
    if not label or len(label) > 220:
        return None
    me = re.match(r"^Myself video,\s*(.+)$", label, re.I)            # your own tile: "Myself video, Abhinav Surattu"
    if me:
        return {"name": me.group(1).strip(), "you": True, "speaking": False, "unmuted": False, "muted": False}
    parts = [p.strip() for p in label.split(",") if p.strip()]
    if len(parts) < 2:
        return None
    name, rest = parts[0], ", ".join(parts[1:])
    if not _STATE.search(rest) or name.lower() in _NOT_NAMES or not _PERSON.match(name):
        return None
    low = rest.lower()
    you = bool(re.search(r"\(you\)", name, re.I))
    name = re.sub(r"\s*\((?:you|guest|external)\)", "", name, flags=re.I).strip()
    return {"name": name, "you": you,
            "speaking": bool(re.search(r"(?<!not )\bspeaking\b", low)),
            "unmuted": bool(re.search(r"\bun-?muted\b", low)),
            "muted": bool(re.search(r"(?<!un)(?<!un-)\bmuted\b", low))}


class TeamsRoster:
    """Polls the Teams meeting window while a meeting is recorded."""

    def __init__(self, t0, you_name="", dump_path=""):
        self.t0, self.you_name = t0, you_name
        self.dump_path = dump_path                  # teams_labels.txt in the meeting's folder: what Teams exposed, for tuning
        self._next_dump = 20.0
        self.names = {}                  # name -> times seen
        self.timeline = []               # {"t", "speaking": [...], "unmuted": [...]} when something changed
        self.reads = 0
        self.samples = []                # labels that look like participant tiles (read or not): for tuning parse_label
        self.ok = False                  # True once a participant tile was read
        self._last = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="teams-roster").start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return {"names": [n for n, c in sorted(self.names.items(), key=lambda kv: -kv[1])],
                    "timeline": list(self.timeline), "reads": self.reads, "ok": self.ok, "samples": list(self.samples)}

    def _maybe_dump(self, labels, window):
        """The whole list of labels Teams exposes, 20 s into the meeting and then every 3 minutes (a meeting's roster and
        captions change as people join). Written only to this laptop, in the meeting's own folder."""
        now = time.time() - self.t0
        if not self.dump_path or now < self._next_dump:
            return
        self._next_dump = now + 180.0
        try:
            seen, lines = set(), []
            for x in labels:
                x = (x or "").strip()
                if x and x not in seen and len(x) <= 240:
                    seen.add(x)
                    lines.append(("PERSON  " if parse_label(x) else "        ") + repr(x))
            with open(self.dump_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== {int(now)} s into the meeting | window {window.get('title')!r} kind={window.get('kind')} | "
                        f"{len(labels)} elements, {len(lines)} distinct labels\n")
                f.write("\n".join(lines[:1500]) + "\n")
        except OSError:
            pass

    # ---- UI Automation ----
    def _setup(self):
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        uia = comtypes.CoCreateInstance(U.CUIAutomation._reg_clsid_, interface=U.IUIAutomation,
                                        clsctx=comtypes.CLSCTX_INPROC_SERVER)
        cache = uia.CreateCacheRequest()
        cache.AddProperty(U.UIA_NamePropertyId)
        return U, uia, cache

    def _labels(self, U, uia, cache, hwnd):
        root = uia.ElementFromHandle(hwnd)
        arr = root.FindAllBuildCache(U.TreeScope_Descendants, uia.CreateTrueCondition(), cache)
        return [arr.GetElement(i).CachedName or "" for i in range(arr.Length)]

    def _run(self):
        try:
            U, uia, cache = self._setup()
        except Exception as e:
            print(f"[names] UI Automation is not available ({type(e).__name__}); names come from the voices only")
            return
        while not self._stop.wait(1.0):
            try:
                w = teams.teams_window()
                if not w or w.get("minimized"):
                    continue
                labels = self._labels(U, uia, cache, w["hwnd"])
                self._maybe_dump(labels, w)
                if w["kind"] == "main":
                    continue                    # the Teams chat window: kept in the dump, not read for people
                people = [p for p in (parse_label(x) for x in labels) if p]
                with self._lock:
                    for x in labels:
                        x = x.strip()
                        if x and len(x) <= 160 and x not in self.samples and len(self.samples) < 80 and _SAMPLE.search(x):
                            self.samples.append(x)
            except Exception as e:
                print(f"[names] read failed: {type(e).__name__}: {e}")
                continue
            now = round(time.time() - self.t0, 1)
            with self._lock:
                self.reads += 1
                if people:
                    self.ok = True
                for p in people:
                    if not p["you"] and p["name"] != self.you_name:
                        self.names[p["name"]] = self.names.get(p["name"], 0) + 1
                state = (tuple(sorted(p["name"] for p in people if p["speaking"])),
                         tuple(sorted(p["name"] for p in people if p["unmuted"] and not p["you"])))
                if state != self._last:
                    self._last = state
                    self.timeline.append({"t": now, "speaking": list(state[0]), "unmuted": list(state[1])})


def dump_tree(path):
    """Writes every labelled element of the Teams meeting window to a text file (for tuning parse_label)."""
    r = TeamsRoster.__new__(TeamsRoster)
    U, uia, cache = r._setup()
    w = teams.teams_window()
    if not w:
        return "No Teams window found."
    labels = r._labels(U, uia, cache, w["hwnd"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"window: {w['title']!r} kind={w['kind']}\n")
        for x in labels:
            if x.strip():
                f.write(f"{'PERSON ' if parse_label(x) else '       '}{x[:200]!r}\n")
    return f"{len(labels)} elements, window {w['title']!r}, written to {path}"
