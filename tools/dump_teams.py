"""Run this DURING a Teams meeting (window open, People list showing) to write what Teams exposes to
Windows accessibility: every label in the meeting window, with the ones CF Minutes reads as participants
marked PERSON. If names are not picked up in a meeting, send the file and the patterns in names.py are tuned.

    .venv\Scripts\python tools\dump_teams.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minutes import names, paths  # noqa: E402

out = os.path.join(paths.HOME, "teams_dump.txt")
paths.ensure()
print(names.dump_tree(out))
