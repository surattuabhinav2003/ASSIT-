"""Starts CF Minutes. Double-click run.bat (or the Desktop shortcut) to open it with no console window."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from minutes.app import main  # noqa: E402

if __name__ == "__main__":
    main()
