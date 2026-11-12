"""Knows when a Teams call is on, and where the Teams window is.

In a call: Windows records which app is using the microphone. While Teams holds it,
LastUsedTimeStop in its ConsentStore entry is older than LastUsedTimeStart (or 0). No polling of
Teams itself and nothing installed into it.

Window: the visible top-level windows of ms-teams.exe (new Teams) or Teams.exe (classic); the
meeting window is preferred over the main chat window.
"""
import ctypes
import ctypes.wintypes as wt
import os
import winreg

user32, kernel32, dwmapi = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.dwmapi
CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
TEAMS_EXE = ("ms-teams.exe", "teams.exe")
MEETING_WORDS = ("meeting", "call", "calling", "presenting", "sharing", "webinar")


def _mic_in_use(key):
    try:
        start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
        stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
        return stop == 0 or start > stop
    except OSError:
        return False


def in_call():
    """True while Teams (new or classic) is using the microphone."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT) as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if "teams" in name.lower():
                    with winreg.OpenKey(root, name) as k:
                        if _mic_in_use(k):
                            return True
            try:
                with winreg.OpenKey(root, "NonPackaged") as np_:
                    j = 0
                    while True:
                        try:
                            name = winreg.EnumKey(np_, j)
                        except OSError:
                            break
                        j += 1
                        if name.lower().endswith("teams.exe"):
                            with winreg.OpenKey(np_, name) as k:
                                if _mic_in_use(k):
                                    return True
            except OSError:
                pass
    except OSError:
        pass
    return False


def meeting_open():
    """True while a Teams meeting or call window (full or compact view) is open.

    The microphone alone is not enough: Teams can let go of it during a call (muted, a pause),
    and the call then looked over -- the panel hid, the session was saved and a new one began,
    over and over in one meeting."""
    try:
        w = teams_window()
    except Exception:
        return False
    return bool(w) and w.get("kind") in ("meeting", "compact")


def _exe_of(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def is_teams(hwnd):
    """True for any window of Teams (meeting, main window, compact view, its pop-ups), or the
    COPILOT_DOCK_TEST_TITLE test window."""
    if not hwnd:
        return False
    root = user32.GetAncestor(hwnd, 3) or hwnd        # GA_ROOTOWNER: a Teams pop-up counts too
    if _exe_of(root) in TEAMS_EXE:
        return True
    test_title = os.getenv("COPILOT_DOCK_TEST_TITLE")
    return bool(test_title) and test_title.lower() in _title(root).lower()


def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _rect(hwnd):
    """The visible frame (without the invisible resize border Windows adds)."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # DWMWA_EXTENDED_FRAME_BOUNDS
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


# The main Teams window's title starts with the section you are in ("Chat | ... | Microsoft Teams").
# A meeting window is "<meeting or person> | Microsoft Teams"; when you switch away from it, Teams
# shows a small floating "Meeting compact view | ...". The panel belongs with the meeting.
MAIN_SECTIONS = ("chat |", "activity |", "teams |", "calendar |", "calls |", "files |", "apps |",
                 "onedrive |", "communities |", "copilot |", "people |", "planner |", "help |")


def _find_test_window(title_part, found):
    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and title_part.lower() in _title(hwnd).lower():
            found.append({"hwnd": hwnd, "title": _title(hwnd), "rect": _rect(hwnd),
                          "minimized": bool(user32.IsIconic(hwnd)), "kind": "meeting"})
        return True
    user32.EnumWindows(visit, 0)


def meeting_title():
    """The meeting's window title, even while that window is minimised ("CloudFuze <> Acme |
    Microsoft Teams"), for suggesting who the call is with; "" when no meeting window is found."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _exe_of(hwnd) in TEAMS_EXE:
            title = _title(hwnd)
            low = title.lower()
            if title and "compact view" not in low and not low.startswith(MAIN_SECTIONS):
                found.append(title)
        return True

    user32.EnumWindows(visit, 0)
    test_title = os.getenv("COPILOT_DOCK_TEST_TITLE")
    if test_title:
        tests = []
        _find_test_window(test_title, tests)
        found = [w["title"] for w in tests] + found
    return found[0] if found else ""


def teams_window():
    """The Teams window to attach to: {hwnd, title, rect, minimized, kind}, kind = "meeting"
    (full meeting window), "compact" (floating mini meeting view) or "main"; or None."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _exe_of(hwnd) in TEAMS_EXE:
            title = _title(hwnd)
            if title:
                low = title.lower()
                kind = ("compact" if "compact view" in low else
                        "main" if low.startswith(MAIN_SECTIONS) else "meeting")
                l, t, r, b = _rect(hwnd)
                found.append({"hwnd": hwnd, "title": title, "rect": (l, t, r, b),
                              "minimized": bool(user32.IsIconic(hwnd)), "kind": kind})
        return True

    user32.EnumWindows(visit, 0)
    test_title = os.getenv("COPILOT_DOCK_TEST_TITLE")
    if test_title:
        # Testing without a real call: treat a window with this title as the meeting window.
        _find_test_window(test_title, found)
    shown = [w for w in found if not w["minimized"] and (w["rect"][2] - w["rect"][0]) > 120]
    area = lambda w: (w["rect"][2] - w["rect"][0]) * (w["rect"][3] - w["rect"][1])  # noqa: E731
    for kind in ("meeting", "compact", "main"):
        pool = sorted((w for w in shown if w["kind"] == kind), key=area, reverse=True)
        if pool:
            return pool[0]
    return found[0] if found else None


# ---- added for CF Minutes: any app using the microphone (Zoom, Google Meet in a browser, Slack huddles...) ----
def apps_using_mic():
    """Names of the apps Windows says are using the microphone right now ("teams", "zoom.exe", "chrome.exe"...)."""
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT) as root:
            def scan(parent, prefix=""):
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(parent, i)
                    except OSError:
                        return
                    i += 1
                    if name == "NonPackaged" and not prefix:
                        with winreg.OpenKey(parent, name) as sub:
                            scan(sub, "np")
                        continue
                    try:
                        with winreg.OpenKey(parent, name) as k:
                            if _mic_in_use(k):
                                out.append(name.replace("#", "\\").rsplit("\\", 1)[-1])
                    except OSError:
                        pass
            scan(root)
    except OSError:
        pass
    return out
