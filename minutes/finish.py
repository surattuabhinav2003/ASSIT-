"""After the meeting: the transcript with names, then the minutes.

  1. The OTHER side's audio is transcribed again with speaker separation (gpt-4o-transcribe-diarize): who
     spoke when, in one consistent set of voices. Long meetings go in pieces; the voices are carried across
     pieces with short reference clips so "Voice 1" stays the same person.
  2. YOUR side: each stretch the microphone heard is transcribed again with the product names and the
     people in the meeting as a hint (the live text is the fallback).
  3. Names: each voice is matched to a person from three kinds of evidence - what Teams showed on screen
     (roster; who was speaking or the only one unmuted), what was said ("I'm Priya", "Thanks, Dave"), and the
     meeting title. A voice with no evidence stays "Speaker 2": it is never guessed. You can rename anyone.
  4. A careful clean-up of mis-heard product and people names (nothing is added or removed).
  5. The minutes.

Everything is written to the meeting's folder on this laptop; only audio and text for these steps go to OpenAI."""
import base64
import concurrent.futures as cf
import io
import json
import os
import re
import time
import wave

import numpy as np

from . import config, store

RATE = 16000
CHUNK_S = 480              # the transcription endpoint takes ~25 MB: 8 minutes of 16 kHz mono is 15 MB
MERGE_GAP_S = 1.5          # a pause shorter than this keeps the same speaker's lines together
MAX_LINE_S = 45


# ---------------------------------------------------------------- vocabulary (CloudFuze product names, mishears)
_VOCAB = None


def vocab():
    global _VOCAB
    if _VOCAB is None:
        from . import paths
        try:
            with open(paths.VOCAB, encoding="utf-8") as f:
                v = json.load(f)
        except (OSError, ValueError):
            v = {}
        rules = []
        for pat, rep in v.get("speech_corrections", []):
            try:
                rules.append((re.compile(pat, re.I), rep))
            except re.error:
                pass
        _VOCAB = {"keywords": v.get("keywords", []), "products": v.get("products", []), "rules": rules}
    return _VOCAB


def fix_people(text, people):
    """A capitalised word that is nearly the first name of someone in the meeting ("Abinav" for Abhinav) is
    spelled the way the roster spells it. Only close matches (very similar, same first letter)."""
    import difflib
    firsts = {}
    for full in people:
        for part in re.split(r"\s+", (full or "").strip())[:1]:
            if len(part) >= 4:
                firsts[part.lower()] = part
    if not firsts:
        return text

    def swap(m):
        w = m.group(0)
        if w.lower() in firsts or not w[0].isupper():
            return w
        hit = difflib.get_close_matches(w.lower(), list(firsts), n=1, cutoff=0.82)
        return firsts[hit[0]] if hit and hit[0][0] == w[0].lower() else w
    return re.sub(r"\b[A-Z][a-z]{3,}\b", swap, text)


def is_noise(text, language="en"):
    """True for a line that has no letters of the language spoken: the model heard a noise and invented a sound ("嗯")."""
    pattern = r"[A-Za-z]" if language == "en" else r"[A-Za-z\u0C00-\u0C7F]"
    return not re.search(pattern, text or "")


def fix_known(text):
    for pat, rep in vocab()["rules"]:
        text = pat.sub(rep, text)
    return text


# ---------------------------------------------------------------- audio helpers
def read_wav(path):
    try:
        with wave.open(path, "rb") as w:
            if w.getframerate() != RATE or w.getnchannels() != 1:
                raise ValueError("unexpected audio format")
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except (EOFError, wave.Error) as e:
        raise ValueError(f"the recording file could not be read ({type(e).__name__})") from e


def to_wav_bytes(x):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.asarray(x, dtype=np.int16).tobytes())
    return buf.getvalue()


def normalised(x):
    """A quiet track (a low Teams volume) is turned up so it transcribes as well as a loud one."""
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak < 60:
        return None                                    # silence
    gain = min(20.0, 0.7 * 32767 / peak)
    return np.clip(x.astype(np.float32) * gain, -32768, 32767).astype(np.int16)


def chunk_points(x, size_s=CHUNK_S):
    """Cut points near every size_s, at the quietest second within 20 s either side."""
    n, pts, pos = len(x), [0], 0
    while n - pos > size_s * RATE * 1.15:
        target = pos + size_s * RATE
        lo, hi = target - 20 * RATE, target + 20 * RATE
        seg = x[lo:hi]
        seg = seg[:len(seg) // RATE * RATE]
        cut = lo + int(np.argmin(np.abs(seg.astype(np.int32)).reshape(-1, RATE).mean(axis=1))) * RATE if len(seg) else target
        pts.append(cut)
        pos = cut
    pts.append(n)
    return pts


def _data_url(x):
    return "data:audio/wav;base64," + base64.b64encode(to_wav_bytes(x)).decode()


# ---------------------------------------------------------------- 1. the other side, with speakers
def diarize(client, path, log=print, size_s=CHUNK_S):
    """[{"voice", "t0", "t1", "text"}] for the other side's track; voices are "V1", "V2"... in order of appearance."""
    x = normalised(read_wav(path))
    if x is None:
        return []
    pts = chunk_points(x, size_s)
    voices, refs, out = {}, {}, []                         # raw label -> "V1"; "V1" -> reference clip
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        piece = x[a:b]
        if len(piece) < RATE * 1.0:
            continue
        kw = {}
        if refs:
            kw = {"extra_body": {"known_speaker_names": list(refs), "known_speaker_references": list(refs.values())}}
        r = None
        for attempt in range(3):
            try:
                r = client.audio.transcriptions.create(
                    model=config.DIARIZE_MODEL, file=("others.wav", to_wav_bytes(piece), "audio/wav"),
                    response_format="diarized_json", chunking_strategy="auto", **kw)
                break
            except Exception as e:
                log(f"[finish] diarize piece {i + 1}/{len(pts) - 1} failed ({type(e).__name__}), try {attempt + 1}")
                if attempt == 1 and kw:
                    kw = {}                                   # the references were refused: carry on without them
                time.sleep(2 + attempt * 3)
        if r is None:
            raise RuntimeError("The transcription service did not answer for part of the meeting.")
        segs = r.model_dump().get("segments", []) if hasattr(r, "model_dump") else r.get("segments", [])
        best = {}                                             # voice -> longest segment, for a reference clip
        for s in segs:
            raw = str(s.get("speaker") or "?")
            if raw in refs:
                v = raw
            else:
                v = voices.setdefault(raw if i == 0 else f"{i}:{raw}", f"V{len(voices) + 1}")
            t0, t1 = a / RATE + float(s["start"]), a / RATE + float(s["end"])
            text = (s.get("text") or "").strip()
            if text:
                out.append({"voice": v, "t0": round(t0, 2), "t1": round(t1, 2), "text": text})
            if t1 - t0 > best.get(v, (0, 0, 0))[0] and t1 - t0 >= 2.5:
                best[v] = (t1 - t0, int(float(s["start"]) * RATE), int(float(s["end"]) * RATE))
        for v, (_, s0, s1) in best.items():                    # references for the next piece (up to 4 voices)
            if v not in refs and len(refs) < 4 and i + 1 < len(pts) - 1:
                clip = piece[s0:min(s1, s0 + 10 * RATE)]
                if len(clip) >= 2 * RATE:
                    refs[v] = _data_url(clip)
    return merge(out)


def merge(segs):
    segs = sorted(segs, key=lambda s: s["t0"])
    out = []
    for s in segs:
        p = out[-1] if out else None
        if p and p["voice"] == s["voice"] and s["t0"] - p["t1"] <= MERGE_GAP_S and s["t1"] - p["t0"] <= MAX_LINE_S:
            p["t1"], p["text"] = s["t1"], (p["text"] + " " + s["text"]).strip()
        else:
            out.append(dict(s))
    return out


# ---------------------------------------------------------------- 2. your side
def hint(names):
    """A short list of names and terms, no sentences: the model must not be able to read it back as speech."""
    v = vocab()
    words = list(dict.fromkeys([*names, *v["keywords"], *v["products"]]))
    return ", ".join(words)[:500]


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _echoes_hint(text, prompt):
    """True when the transcription is just the hint read back (it does that on near-silence): never a real line."""
    t, p = _norm(text), _norm(prompt)
    return bool(t) and (t in p or (len(t.split()) <= 12 and all(w in p.split() for w in t.split())))


def retranscribe(client, path, spans, names, log=print, language="en", min_s=0.9):
    """{span index: text} for stretches of a track; a stretch that fails is left out (the text already there stays)."""
    x = read_wav(path)
    prompt = hint(names)
    lang = {"en": "en", "te": "te"}.get(language)            # mixed: no language, the model decides

    def one(i):
        t0, t1 = spans[i]
        if t1 - t0 < min_s:
            return i, None                                 # too short to be worth a second look: the live text stays
        a, b = max(0, int((t0 - 0.25) * RATE)), min(len(x), int((t1 + 0.4) * RATE))
        piece = normalised(x[a:b])
        if piece is None or len(piece) < RATE * 0.4:
            return i, None
        for attempt in range(2):
            try:
                extra = {"language": lang} if lang else {}
                r = client.audio.transcriptions.create(model="gpt-4o-transcribe", prompt=prompt, **extra,
                                                       file=("you.wav", to_wav_bytes(piece), "audio/wav"))
                text = (r.text or "").strip()
                return i, (None if _echoes_hint(text, prompt) else text)
            except Exception as e:
                log(f"[finish] re-transcribing your speech failed ({type(e).__name__})")
                time.sleep(1.5)
        return i, None

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        return {i: t for i, t in pool.map(one, range(len(spans))) if t}


# ---------------------------------------------------------------- 3. names
def evidence(voice_lines, roster, you_name):
    """Per voice, per name from the Teams roster: seconds the name was speaking / the only other person unmuted
    while that voice talked. Plain numbers for the model to weigh, not a decision."""
    tl = roster.get("timeline", []) if roster else []
    out = {}
    if not tl:
        return out
    for voice, lines in voice_lines.items():
        score = {}
        for ln in lines:
            for j, e in enumerate(tl):
                e0 = e["t"]
                e1 = tl[j + 1]["t"] if j + 1 < len(tl) else ln["t1"]
                ov = max(0.0, min(ln["t1"], e1) - max(ln["t0"], e0))
                if ov <= 0:
                    continue
                others = [n for n in e["unmuted"] if n != you_name]
                for n in e["speaking"]:
                    if n != you_name:
                        score.setdefault(n, {"speaking_s": 0.0, "only_unmuted_s": 0.0})["speaking_s"] += ov
                if len(others) == 1 and not e["speaking"]:
                    score.setdefault(others[0], {"speaking_s": 0.0, "only_unmuted_s": 0.0})["only_unmuted_s"] += ov
        out[voice] = {n: {k: round(v, 1) for k, v in d.items()} for n, d in score.items()}
    return out


def _chat_json(client, system, user, effort="low"):
    kw = {"model": config.MOM_MODEL, "response_format": {"type": "json_object"},
          "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    last = None
    for opts in ({"reasoning_effort": effort}, {}):
        try:
            r = client.chat.completions.create(**kw, **opts)
            return json.loads(r.choices[0].message.content)
        except Exception as e:                      # a model that takes no reasoning_effort: retry without it
            last = e
    raise last


NAMING = """You match the voices in a recorded meeting to the people's real names.
You get: who the user is (their voice is already known, ignore it), the people Teams showed in the meeting (may be
empty or incomplete), per-voice evidence from Teams (seconds a name was shown speaking, or was the only other
person unmuted, while that voice talked), the meeting title, and each voice's lines.
Rules:
- Use a name only when the evidence supports it: the voice introduces itself ("I'm Priya"), is answered by name
  right after speaking ("Thanks Priya"), or the Teams evidence points to it. Words like "Dave, can you confirm...?"
  are said TO Dave: the speaker is not Dave, but whoever answers next may be.
- Spell names the way the Teams roster spells them. Never invent a surname.
- If the evidence is weak or conflicting, give null. A wrong name is worse than none.
- One name per voice; two voices never share a name.
Reply JSON: {"voices": {"V1": {"name": "Priya Shah" | null, "confidence": "high" | "medium" | "low", "why": "<short>"}}}"""


def name_voices(client, voice_lines, roster, you_name, title, log=print):
    """{"V1": "Priya Shah" or None} plus {"V1": confidence}."""
    names = [n for n in (roster or {}).get("names", []) if n != you_name]
    if not voice_lines:
        return {}, {}
    if len(voice_lines) == 1 and len(names) == 1:
        v = next(iter(voice_lines))
        return {v: names[0]}, {v: "high"}                # a call with one other person
    sample = {v: [f"[{l['t0']:.0f}s] {l['text']}" for l in lines[:25]] for v, lines in voice_lines.items()}
    payload = {"user": you_name, "teams_roster": names, "teams_evidence": evidence(voice_lines, roster, you_name),
               "meeting_title": title, "voices": sample}
    try:
        r = _chat_json(client, NAMING, json.dumps(payload, ensure_ascii=False), effort="medium")
    except Exception as e:
        log(f"[finish] naming failed ({type(e).__name__}); voices stay unnamed")
        return {}, {}
    out, conf = {}, {}
    taken = set()
    for v, d in (r.get("voices") or {}).items():
        n = (d or {}).get("name")
        if v in voice_lines and isinstance(n, str) and n.strip() and n.strip() not in taken and d.get("confidence") in ("high", "medium"):
            out[v], conf[v] = n.strip(), d["confidence"]
            taken.add(n.strip())
    return out, conf


# ---------------------------------------------------------------- 4. clean-up of mis-heard names
POLISH = """You correct speech-to-text mistakes in a meeting transcript. Change ONLY words that are clearly a mis-heard
person name, company name or product name, using the names and terms given. Fix obvious sound-alike errors
("Abinav" for Abhinav, "i t" for IT). Do not rephrase, add, remove, summarise or reorder anything, and do not fix
grammar or style. If a line needs no fix, return it exactly as given.
Reply JSON: {"lines": ["<line 1>", "<line 2>", ...]} with exactly as many lines, in the same order."""


def polish(client, rows, names, you_name, log=print):
    texts = [r["text"] for r in rows]
    fixed = []
    for i in range(0, len(texts), 60):
        batch = texts[i:i + 60]
        try:
            v = vocab()
            r = _chat_json(client, POLISH, json.dumps({
                "people": [n for n in [you_name, *names] if n],
                "terms": list(dict.fromkeys(v["keywords"] + v["products"])),
                "lines": batch}, ensure_ascii=False))
            got = r.get("lines")
            ok = isinstance(got, list) and len(got) == len(batch) and all(isinstance(g, str) for g in got)
            if ok:
                # a line that changed by more than a few words is not a name fix: keep what was heard
                got = [g if _small_change(o, g) else o for o, g in zip(batch, got)]
        except Exception as e:
            log(f"[finish] clean-up skipped ({type(e).__name__})")
            ok = False
        fixed += got if ok else batch
    for r, t in zip(rows, fixed):
        r["text"] = fix_people(fix_known(t), [you_name, *names])
    return rows


def _small_change(a, b):
    wa, wb = a.split(), b.split()
    if abs(len(wa) - len(wb)) > 2:
        return False
    return sum(1 for x, y in zip(wa, wb) if x != y) <= max(3, len(wa) // 4)


# ---------------------------------------------------------------- Telugu and English
LATINISE = """You clean up a transcript of a business meeting where people speak Telugu, English, or both. The speech-to-text wrote English
words in Telugu letters (for example "ఫ్రైడే", "పైలట్ స్టార్ట్", "యూజర్ లిస్ట్", "డేవ్"). Rewrite each line so that:
- English words, product names and people's names that were spoken in English are written in English letters ("Friday", "pilot start", "user list", "Dave", "SharePoint").
- Telugu words stay in Telugu script, unchanged.
- Nothing is added, removed, translated or reordered. Keep the punctuation. A line that is all English or all Telugu words with no English in it stays as it is.
Reply JSON: {"lines": [...]} with exactly as many lines, in the same order."""


def latinise(client, rows, names, log=print):
    """Telugu and mixed meetings: English words back into English letters."""
    v = vocab()
    out = []
    texts = [r["text"] for r in rows]
    for i in range(0, len(texts), 40):
        batch = texts[i:i + 40]
        try:
            r = _chat_json(client, LATINISE, json.dumps({"known_names_and_terms": [*names, *v["keywords"], *v["products"]],
                                                         "lines": batch}, ensure_ascii=False))
            got = r.get("lines")
            ok = isinstance(got, list) and len(got) == len(batch) and all(isinstance(g, str) and g.strip() for g in got)
        except Exception as e:
            log(f"[finish] English words in Telugu text left as heard ({type(e).__name__})")
            ok = False
        out += got if ok else batch
    for r, t in zip(rows, out):
        r["text"] = t
    return rows


# ---------------------------------------------------------------- 5. the minutes
MOM_SYSTEM = """You write short, accurate minutes of a meeting from its transcript, for the people who were there and for
colleagues who were not. Plain, neutral professional English. Facts only: use what was said, never add, assume or
tidy up a fact. If something is unclear, say so rather than guess.
Keep it SHORT:
- "summary": 3 to 6 bullets, each one sentence: what the meeting was about and what was agreed.
- "decisions": what was decided or agreed (each one line). Empty list if none.
- "actions": each {"owner": "<name as in the transcript, or 'Unassigned'>", "task": "<what>", "due": "<date or day as said, or ''>"}.
- "open_questions": questions or items left open (each one line).
- "title": a short meeting title (max 8 words) if the given title is generic; otherwise return the given title.
Dates: keep them as spoken ("Friday", "next Tuesday") unless an exact date was said; the meeting date is given for reference.
Speaker names in the transcript are the best known; a speaker shown as "Speaker N" has no name yet: keep that label.
Reply JSON: {"title": "", "summary": [], "decisions": [], "actions": [], "open_questions": []}"""

MOM_IN_ENGLISH = ("\nThe meeting may be in Telugu, English or both. Write the minutes in ENGLISH regardless; keep people's names and product "
                  "names as they are. Dates and days stay as said.")
MOM_IN_MEETING = ("\nThe meeting may be in Telugu, English or both. Write the minutes in the language of the meeting: Telugu in Telugu "
                  "script where Telugu was spoken, with English terms, names and products in English letters.")


def make_minutes(client, meta, rows, log=print, minutes_language="english"):
    system = MOM_SYSTEM + (MOM_IN_MEETING if minutes_language == "meeting" and meta.get("language", "en") != "en" else
                           MOM_IN_ENGLISH if meta.get("language", "en") != "en" else "")
    if not rows:
        return {"title": meta.get("title") or "Meeting", "summary": ["Nothing was said."], "decisions": [], "actions": [],
                "open_questions": []}
    when = time.strftime("%A %d %B %Y", time.localtime(meta.get("started") or time.time()))
    transcript = "\n".join(f"[{int(r['t0'] // 60):02d}:{int(r['t0'] % 60):02d}] {r['speaker']}: {r['text']}" for r in rows)
    # a very long meeting is summarised in parts, then the parts are joined
    if len(transcript) > 90000:
        parts = [transcript[i:i + 80000] for i in range(0, len(transcript), 80000)]
        partials = [_chat_json(client, system, json.dumps({"meeting_title": meta.get("title"), "date": when,
                                                               "transcript": p}, ensure_ascii=False)) for p in parts]
        transcript = "Minutes of the parts of this meeting:\n" + json.dumps(partials, ensure_ascii=False)
    m = _chat_json(client, system, json.dumps({"meeting_title": meta.get("title"), "date": when,
                                                   "transcript": transcript}, ensure_ascii=False), effort="medium")
    return {"title": str(m.get("title") or meta.get("title") or "Meeting"),
            "summary": [str(x) for x in m.get("summary") or []],
            "decisions": [str(x) for x in m.get("decisions") or []],
            "actions": [{"owner": str(a.get("owner") or "Unassigned"), "task": str(a.get("task") or ""), "due": str(a.get("due") or "")}
                        for a in m.get("actions") or [] if isinstance(a, dict) and a.get("task")],
            "open_questions": [str(x) for x in m.get("open_questions") or []],
            "generated": time.time(), "model": config.MOM_MODEL}


# ---------------------------------------------------------------- the whole pass
GENERIC_TITLES = ("meeting", "microsoft teams", "meeting now", "call")


def run(client, mid, roster=None, you_name="", status=lambda s: None, log=print):
    """Reads what was recorded, writes the final transcript, the minutes and the meeting's status."""
    meta = store.get(mid)
    live = store.lines(mid)
    others_p, you_p = store.track_path(mid, "others"), store.track_path(mid, "you")
    you_name = you_name or meta.get("you") or "You"
    roster_names = [n for n in (roster or {}).get("names", []) if n != you_name]
    rows = []
    language = meta.get("language") or "en"

    you_live = [r for r in live if r["side"] == "you" and r.get("text", "").strip()]
    status("Transcribing both sides")
    pool = cf.ThreadPoolExecutor(max_workers=2)
    f_others = pool.submit(diarize, client, others_p, log) if os.path.isfile(others_p) else None
    f_you = (pool.submit(retranscribe, client, you_p, [(r["t0"], r["t1"]) for r in you_live], roster_names, log, language)
             if os.path.isfile(you_p) and you_live else None)
    voices = None
    if f_others:
        try:
            voices = f_others.result()
        except Exception as e:
            log(f"[finish] speaker separation failed ({type(e).__name__}: {e}); using the live transcript for the other side")
    if voices is None:
        voices = [{"voice": "V1", "t0": r["t0"], "t1": r["t1"], "text": r["text"]} for r in live if r["side"] == "them"]
        single = True
    else:
        single = False
        if language != "en" and voices:
            # The separation model writes Telugu in Latin letters ("Weeklow"): useful for WHO spoke WHEN, not as text.
            # Each stretch is transcribed again with the better model, in the language of the meeting.
            status("Transcribing Telugu")
            again = retranscribe(client, others_p, [(v["t0"], v["t1"]) for v in voices], roster_names, log, language, min_s=0.4)
            voices = [dict(v, text=again[i]) for i, v in enumerate(voices) if i in again]
    by_voice = {}
    for v in voices:
        by_voice.setdefault(v["voice"], []).append(v)

    # your side
    you_text = f_you.result() if f_you else {}
    pool.shutdown(wait=False)
    for i, r in enumerate(you_live):
        rows.append({"side": "you", "speaker": you_name, "t0": r["t0"], "t1": r["t1"], "text": you_text.get(i) or r["text"]})

    # names
    status("Matching voices to names")
    named, conf = ({}, {}) if single else name_voices(client, by_voice, roster, you_name, meta.get("title", ""), log)
    order = {v: i + 1 for i, v in enumerate(by_voice)}
    for v in voices:
        label = named.get(v["voice"]) or (f"Speaker {order[v['voice']]}" if not single else "Others")
        rows.append({"side": "them", "speaker": label, "t0": v["t0"], "t1": v["t1"], "text": v["text"]})
    rows = [r for r in rows if r["text"].strip() and not is_noise(r["text"], language)]
    rows.sort(key=lambda r: (r["t0"], r["t1"]))
    for i, r in enumerate(rows):
        r["id"] = i + 1
    if language != "en":
        status("Writing English words in English")
        rows = latinise(client, rows, [you_name, *roster_names], log)
    rows = polish_safe(client, rows, roster_names, you_name, status, log)
    store.save_lines(mid, rows)

    # the minutes
    status("Writing the minutes")
    attendees = list(dict.fromkeys([you_name] + [r["speaker"] for r in rows if not r["speaker"].startswith(("Speaker ", "Others"))]))
    from . import config
    mom = make_minutes(client, meta, rows, log, config.load_settings().get("minutes_language", "english"))
    mom["speaker_names"] = {"matched": named, "confidence": conf}
    store.save_mom(mid, mom)
    title = meta.get("title") or ""
    if not title or title.lower().replace(" | microsoft teams", "").strip() in GENERIC_TITLES:
        title = mom.get("title") or title
    store.update(mid, status="ready", attendees=attendees, title=title or "Meeting", error="")
    return rows, mom


def polish_safe(client, rows, names, you_name, status, log):
    status("Checking names and product terms")
    try:
        return polish(client, rows, names, you_name, log)
    except Exception as e:
        log(f"[finish] clean-up failed ({type(e).__name__})")
        return rows
