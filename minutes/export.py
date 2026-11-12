"""The minutes as Markdown or a Word document, and the transcript as text."""
import time

from . import store


def _stamp(sec):
    sec = int(sec or 0)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def _facts(meta):
    start = meta.get("started") or time.time()
    mins = round(((meta.get("ended") or start) - start) / 60)
    return {"date": time.strftime("%A %d %B %Y, %H:%M", time.localtime(start)), "minutes": mins,
            "attendees": ", ".join(meta.get("attendees") or []) or "Not recorded"}


def minutes_markdown(mid, with_transcript=False):
    meta, mom = store.get(mid), store.mom(mid) or {}
    f = _facts(meta)
    out = [f"# {meta.get('title') or 'Meeting'}", "", f"**Date:** {f['date']}  ",
           f"**Length:** {f['minutes']} min  ", f"**Attendees:** {f['attendees']}", ""]
    if mom.get("summary"):
        out += ["## Summary", *[f"- {x}" for x in mom["summary"]], ""]
    if mom.get("decisions"):
        out += ["## Decisions", *[f"- {x}" for x in mom["decisions"]], ""]
    if mom.get("actions"):
        out += ["## Action items", "", "| Owner | Action | Due |", "|---|---|---|"]
        out += [f"| {a['owner']} | {a['task']} | {a.get('due') or ''} |" for a in mom["actions"]]
        out += [""]
    if mom.get("open_questions"):
        out += ["## Open questions", *[f"- {x}" for x in mom["open_questions"]], ""]
    if with_transcript:
        out += ["## Transcript", "", transcript_text(mid)]
    return "\n".join(out).rstrip() + "\n"


def transcript_text(mid):
    return "\n".join(f"[{_stamp(r['t0'])}] {r['speaker']}: {r['text']}" for r in store.lines(mid))


def minutes_docx(mid, path, with_transcript=False):
    from docx import Document
    from docx.shared import Pt, RGBColor
    meta, mom = store.get(mid), store.mom(mid) or {}
    f = _facts(meta)
    blue = RGBColor(0x01, 0x29, 0xAC)
    d = Document()
    d.styles["Normal"].font.name = "Calibri"
    d.styles["Normal"].font.size = Pt(11)
    h = d.add_heading(meta.get("title") or "Meeting", level=0)
    for r in h.runs:
        r.font.color.rgb = blue
    p = d.add_paragraph()
    for label, value in (("Date", f["date"]), ("Length", f"{f['minutes']} min"), ("Attendees", f["attendees"])):
        run = p.add_run(f"{label}: ")
        run.bold = True
        p.add_run(value + "\n")

    def section(title, items):
        if not items:
            return
        hh = d.add_heading(title, level=1)
        for r in hh.runs:
            r.font.color.rgb = blue
        for x in items:
            d.add_paragraph(x, style="List Bullet")

    section("Summary", mom.get("summary"))
    section("Decisions", mom.get("decisions"))
    if mom.get("actions"):
        hh = d.add_heading("Action items", level=1)
        for r in hh.runs:
            r.font.color.rgb = blue
        t = d.add_table(rows=1, cols=3)
        t.style = "Light Grid Accent 1"
        for i, c in enumerate(("Owner", "Action", "Due")):
            t.rows[0].cells[i].text = c
        for a in mom["actions"]:
            cells = t.add_row().cells
            cells[0].text, cells[1].text, cells[2].text = a["owner"], a["task"], a.get("due") or ""
    section("Open questions", mom.get("open_questions"))
    if with_transcript:
        hh = d.add_heading("Transcript", level=1)
        for r in hh.runs:
            r.font.color.rgb = blue
        for r in store.lines(mid):
            p = d.add_paragraph()
            a = p.add_run(f"[{_stamp(r['t0'])}] {r['speaker']}: ")
            a.bold = True
            p.add_run(r["text"])
    d.save(path)
    return path
