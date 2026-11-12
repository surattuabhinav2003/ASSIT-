"""Starts CF Minutes. Use run.bat (it sets everything up the first time) or the Desktop shortcut."""
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)


def _say(text):
    """No console here (pythonw): a message box is the only way to tell the person."""
    try:
        ctypes.windll.user32.MessageBoxW(0, text, "CF Minutes", 0x10)
    except Exception:
        print(text)


if __name__ == "__main__":
    try:
        from minutes.app import main
    except ImportError as e:
        _say(f"A required package is missing ({e.name or e}).\n\nRun setup.bat in the CF Minutes folder, then start it again.")
        sys.exit(1)
    main()
