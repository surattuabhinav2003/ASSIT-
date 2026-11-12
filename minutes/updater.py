"""Keeps a laptop on the latest code: the app was installed from the Git repository, so an update is a
fast-forward pull of `main`. Checked at start and every few hours; applied (and the app restarted) only when
nothing is being recorded or written. A laptop that cannot update says why, never fails silently."""
import os
import subprocess
import sys
import threading
import time

from . import paths

EVERY_S = 4 * 3600
NO_WINDOW = 0x08000000


def _git(*args, timeout=60):
    return subprocess.run(["git", *args], cwd=paths.CODE, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW)


def installed_from_git():
    return os.path.isdir(os.path.join(paths.CODE, ".git"))


def head():
    r = _git("rev-parse", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def check():
    """{"behind": n, "error": text}. Fetches only; changes nothing on disk."""
    if not installed_from_git():
        return {"behind": 0, "error": "This copy was not installed from Git, so it cannot update itself."}
    try:
        f = _git("fetch", "--quiet", "origin", "main")
        if f.returncode != 0:
            return {"behind": 0, "error": _why(f.stderr)}
        n = _git("rev-list", "--count", "HEAD..origin/main")
        return {"behind": int(n.stdout.strip() or 0), "error": ""}
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        return {"behind": 0, "error": f"{type(e).__name__}"}


def apply():
    """Fast-forward to origin/main, install new packages if the list changed. Returns "" or the reason it failed."""
    before = _git("rev-parse", "HEAD").stdout.strip()
    r = _git("pull", "--ff-only", "--quiet", "origin", "main", timeout=180)
    if r.returncode != 0:
        return _why(r.stderr)
    after = _git("rev-parse", "HEAD").stdout.strip()
    if before != after:
        changed = _git("diff", "--name-only", before, after).stdout
        if "requirements.txt" in changed:
            py = os.path.join(paths.CODE, ".venv", "Scripts", "python.exe")
            subprocess.run([py if os.path.isfile(py) else sys.executable, "-m", "pip", "install", "-q", "-r",
                            os.path.join(paths.CODE, "requirements.txt")], cwd=paths.CODE, timeout=600, creationflags=NO_WINDOW)
    return ""


def _why(text):
    t = (text or "").lower()
    if "not possible to fast-forward" in t or "diverg" in t:
        return "This copy has changes of its own, so it cannot move to the latest version."
    if "overwritten" in t or "local changes" in t:
        return "Files in this copy were edited, so the update was not applied."
    if "could not resolve" in t or "unable to access" in t or "network" in t:
        return "The update server could not be reached."
    if "authentication" in t or "denied" in t or "not found" in t:
        return "The update server refused the request."
    return "The update did not finish."


def restart():
    py = os.path.join(paths.CODE, ".venv", "Scripts", "pythonw.exe")
    subprocess.Popen([py if os.path.isfile(py) else sys.executable, os.path.join(paths.CODE, "run.py"), "--restarted"],
                     cwd=paths.CODE, creationflags=NO_WINDOW | 0x00000008)


class Loop:
    """state: {"updated": bool, "error": str, "version": short hash}"""
    def __init__(self, idle, quit_app, log=print):
        self.idle, self.quit_app, self.log = idle, quit_app, log
        self.state = {"updated": False, "error": "", "version": head()}
        threading.Thread(target=self._run, daemon=True, name="updater").start()

    def _run(self):
        time.sleep(20)
        while True:
            try:
                c = check()
                self.state["error"] = c["error"]
                if c["behind"] and not c["error"]:
                    why = apply()
                    self.state["error"] = why
                    if not why:
                        self.state.update(updated=True, version=head())
                        self.log(f"[update] now on {self.state['version']}")
                if self.state["updated"]:
                    while not self.idle():
                        time.sleep(60)
                    restart()
                    time.sleep(1)
                    self.quit_app()
                    return
            except Exception as e:
                self.state["error"] = f"{type(e).__name__}"
            time.sleep(EVERY_S)
