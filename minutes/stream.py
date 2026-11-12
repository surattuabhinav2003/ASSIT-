"""(Copied from CloudFuze Assist, audio/stream.py.) Streaming transcription: one OpenAI realtime transcription session per side of the call.

Audio is streamed while the person is still talking, so words arrive live (~0.7 s behind the
voice) and the full sentence is ready ~0.3 s after they stop — instead of waiting for silence,
uploading a file and transcribing it (~1.7 s).

The local voice detector in audio.py decides where an utterance ends: it calls start() when
speech begins, audio() for every frame, and end() after a short silence. end() commits the
utterance; a mic utterance that was really the customer's voice through the speakers is
cleared instead, so it never appears.
"""
import base64
import collections
import os
import threading
import time
import traceback

from openai import OpenAI

RATE = 24000                      # realtime sessions take 24 kHz 16-bit mono PCM
PREROLL_S = 0.3                   # audio kept from just before speech starts, so first words aren't clipped
BACKLOG_FRAMES = int(10 * 1000 / 30)   # speech held while connecting: 10 s of 30 ms frames
# Never lose what the customer says: each utterance's audio is kept until its final words are in.
# If the live connection drops during or after it, its final words do not come within
# WATCHDOG_S, or the final words are much shorter than what was already on screen, the kept audio
# is transcribed again in one piece (recover) and the whole sentence is put back.
WATCHDOG_S = 6.0
SHORTER_RATIO = 0.6               # final has fewer than 60% of the words shown live: recover it
KEEP_AUDIO_S = 90                 # forget kept audio older than this

# gpt-live-transcribe: product names come out right (KEYWORDS) and it is punctuated. Measured
# 2026-10-14: gpt-realtime-whisper reaches the text ~0.3 s sooner but is NOT faster end to end
# (the answer dominates), and it heard "ShareFile" as "shapefile" -> a wrong answer. Accuracy wins.
# fix_names() below is a second safety net for either model.
from . import config
MODEL = config.LIVE_MODEL
SETTLE_S = 0.05                   # after end(): first look for the sentence
QUIET_S = 0.3 if MODEL != "gpt-realtime-whisper" else 0.15   # ...then wait until no new words for this long
MAX_WAIT_S = 0.7 if MODEL != "gpt-realtime-whisper" else 0.35  # ...but never longer (live-transcribe's last word lands ~0.9 s after the voice stops)
# Loaded from the shared database by store.load_reference at start-up:
#   KEYWORDS  transcription_keywords: words the transcription model is told to expect.
#   _NAMES    speech_corrections: (compiled regex, replacement) for product names speech-to-text
#             mishears ("chair file" -> ShareFile, "ignite" next to migration words -> Egnyte).
KEYWORDS = []
_NAMES = []


def fix_names(text):
    """Spell product names the way the docs do, so search and the agent find them."""
    for pattern, name in _NAMES:
        text = pattern.sub(name, text)
    return text


LANGUAGE = "en"                   # set by the session: "en", "te", or None to let the model decide (Telugu and English mixed)


def _transcription_config():
    cfg = {"model": MODEL}
    if LANGUAGE:
        cfg["language"] = LANGUAGE
    if MODEL == "gpt-realtime-whisper":
        cfg["delay"] = os.getenv("COPILOT_STREAM_DELAY", "minimal")
    elif MODEL in ("gpt-live-transcribe", "gpt-transcribe"):
        cfg["keywords"] = KEYWORDS
    return cfg


def _new_stats():
    return {"heard": 0, "sent": 0, "final": 0, "dropped": 0, "recovered": 0, "shortened": 0, "lost": 0,
            "reconnects": 0}


def _wav(pcm):
    """24 kHz 16-bit mono PCM -> WAV bytes."""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class StreamSide:
    """One side of the call ("them" or "you") streaming into its own realtime session.

    on_start(line_id)            the voice detector heard speech start (before any words arrive)
    on_end(line_id, kept)        ...and stop; kept=False when the utterance was discarded
    on_partial(line_id, text)    the line so far, called as words arrive
    on_ready(line_id, text)      the utterance ended: text to act on (may be refined later)
    on_final(line_id, text)      the finished transcript for the line
    """

    def __init__(self, client: OpenAI, who, on_partial, on_ready, on_final, on_error=None,
                 on_start=None, on_end=None, recover=None):
        self.client, self.who = client, who
        self.recover = recover              # recover(wav_bytes) -> text: one-piece transcription
        self._audio = {}                    # line id -> its audio (PCM), until its final words are in
        self._born = {}                     # line id -> when it started (to forget old audio)
        self._waiting = set()               # committed, final words not in yet
        self._broken = set()                # the live connection dropped during it: recover from audio
        self._recovering = set()
        self._readied = set()               # on_ready already called
        self._shown = {}                    # line id -> most words shown live
        self.stats = _new_stats()
        self.on_partial, self.on_ready, self.on_final = on_partial, on_ready, on_final
        self.on_error = on_error or (lambda msg: None)
        self.on_start = on_start or (lambda line: None)
        self.on_end = on_end or (lambda line, kept: None)
        self._conn = None
        self._wake = threading.Event()      # cut the reconnect wait short (a call is starting)
        self._lock = threading.Lock()
        self._preroll = collections.deque(maxlen=int(PREROLL_S * 1000 / 30) + 1)
        self._backlog = []                  # speech heard while not connected
        self._deferred = []                 # utterances that ended before the connection was up
        self._speaking = False
        self._seq = 0
        self._line = None          # line id of the utterance being spoken
        self._text = {}            # line id -> text so far
        self._last_delta = {}      # line id -> time the latest words arrived
        self._ended = {}           # line id -> time the voice stopped (for measuring answer speed)
        self._items = {}           # realtime item id -> line id
        self._pending = collections.deque()   # (line id, time) committed, waiting for their item id
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    # ---------- audio in (from the voice detector) ----------
    def audio(self, pcm24):
        if not self._speaking:
            self._preroll.append(pcm24)
            return
        self._send(pcm24)

    def start(self):
        with self._lock:
            self._seq += 1
            self._line = f"{self.who[0]}{self._seq}"
            self._text[self._line] = ""
            self._speaking = True
            line = self._line
            self._audio[line] = bytearray()
            self._born[line] = time.time()
            self.stats["heard"] += 1
            for old in [x for x, t in self._born.items() if time.time() - t > KEEP_AUDIO_S]:
                self._forget(old)
        for chunk in list(self._preroll):
            self._send(chunk)
        self._preroll.clear()
        self.on_start(line)

    def end(self, keep=True):
        with self._lock:
            line, self._line, self._speaking = self._line, None, False
        if line is None:
            return
        if not keep:
            self._forget(line)
            self.stats["dropped"] += 1
        elif line in self._broken:
            # The connection dropped while it was being said: the server has only part of it.
            try:
                self._conn and self._conn.input_audio_buffer.clear()
            except Exception:
                pass
            self._backlog = []
            self._ended[line] = time.time()
            self.on_end(line, True)
            self._recover_later(line, "the connection dropped during it")
            return
        conn = self._conn
        committed = False
        if conn is None:
            if keep and self._backlog:
                # Said while connecting: committed as soon as the connection is up (_run).
                self._deferred.append(line)
                self._ended[line] = time.time()
                self.on_end(line, True)
            else:
                self._backlog = []
                self._text.pop(line, None)
                self.on_end(line, False)
            return
        try:
            if keep:
                # Only an utterance actually sent waits for its transcript. Queuing one that never
                # reached the server shifted every later transcript onto the wrong line.
                entry = (line, time.time())
                self._pending.append(entry)        # before the commit: its confirmation can be quick
                try:
                    conn.input_audio_buffer.commit()
                except Exception:
                    if entry in self._pending:
                        self._pending.remove(entry)
                    raise
                self._ended[line] = time.time()
                committed = True
                self._waiting.add(line)
                self.stats["sent"] += 1
                threading.Timer(SETTLE_S, self._ready, args=(line,)).start()
                threading.Timer(WATCHDOG_S, self._watchdog, args=(line,)).start()
            else:
                conn.input_audio_buffer.clear()
        except Exception as e:
            self.on_error(f"{type(e).__name__}")
        if not committed:
            self._text.pop(line, None)
        self.on_end(line, committed)

    def reset(self):
        """A new call: forget every line of the last one and start on a fresh connection, so
        nothing from before can be matched to what is said now (an idle connection between calls
        is also the one most likely to have been dropped)."""
        with self._lock:
            self._line, self._speaking = None, False
            self._pending.clear()
            self._items.clear()
            self._text.clear()
            self._last_delta.clear()
            self._ended.clear()
            self._backlog, self._deferred = [], []
            self._audio.clear()
            self._born.clear()
            self._waiting.clear()
            self._broken.clear()
            self._recovering.clear()
            self._readied.clear()
            self._shown.clear()
            self.stats = _new_stats()
        self._preroll.clear()
        conn, self._conn = self._conn, None
        self._wake.set()
        try:
            conn and conn.close()
        except Exception:
            pass

    def _ready(self, line):
        # Hand the sentence over once words have stopped arriving for QUIET_S (the last word
        # lands ~0.3 s after the voice stops), or after MAX_WAIT_S at most — never on a word
        # that is still on its way.
        waited = 0.0
        while waited < MAX_WAIT_S and time.time() - self._last_delta.get(line, 0) < QUIET_S:
            time.sleep(0.03)
            waited += 0.03
        if line in self._recovering:
            return                     # its words come from the kept audio instead
        text = fix_names(self._text.get(line, "").strip())
        if text:
            self._readied.add(line)
            self.on_ready(line, text, self._ended.get(line, time.time()))

    def _send(self, pcm24):
        line = self._line
        if line is not None and line in self._audio:
            self._audio[line] += pcm24
        conn = self._conn
        if conn is None:
            # (Re)connecting - e.g. the fresh connection every call starts with: keep what the
            # customer says meanwhile (up to 10 s) and send it once connected.
            if self._speaking and len(self._backlog) < BACKLOG_FRAMES:
                self._backlog.append(pcm24)
            return
        try:
            conn.input_audio_buffer.append(audio=base64.b64encode(pcm24).decode())
        except Exception:
            pass   # the reader thread reconnects

    # ---------- never lose a sentence ----------
    def _forget(self, line):
        self._audio.pop(line, None)
        self._born.pop(line, None)
        self._waiting.discard(line)
        self._shown.pop(line, None)

    def _watchdog(self, line):
        """Its final words should be in by now; if not, recover it from the kept audio."""
        if line in self._waiting and line not in self._recovering:
            self._recover_later(line, f"no final words after {WATCHDOG_S:.0f} s")

    def _recover_later(self, line, why):
        if line in self._recovering or line not in self._audio:
            return
        self._recovering.add(line)
        self._waiting.discard(line)
        threading.Thread(target=self._recover, args=(line, why), daemon=True).start()

    def _recover(self, line, why):
        pcm = bytes(self._audio.get(line) or b"")
        if not self.recover or len(pcm) < RATE * 2 * 0.3:      # nothing to send, or under 0.3 s
            self.stats["lost"] += 1
            print(f"[stream:{self.who}] {line}: words lost ({why}) - no audio to recover from")
            return
        try:
            text = fix_names((self.recover(_wav(pcm)) or "").strip())
        except Exception as e:
            self.stats["lost"] += 1
            print(f"[stream:{self.who}] {line}: recovery failed ({why}): {type(e).__name__}: {e}")
            return
        self.stats["recovered"] += 1
        print(f"[stream:{self.who}] {line}: recovered from its audio ({why}): {len(text.split())} words")
        self._forget(line)
        if not text:
            return
        self._text[line] = text
        if line not in self._readied:
            self._readied.add(line)
            self.on_ready(line, text, self._ended.get(line, time.time()))
        self.on_final(line, text)

    def stop(self):
        self._stop.set()
        try:
            self._conn and self._conn.close()
        except Exception:
            pass

    # ---------- transcripts out ----------
    def _committed(self, item_id):
        """The server confirms a commit with the item's id, in commit order. An item already known
        (its words streamed while the person was talking) just leaves the waiting list; otherwise
        it is paired with the oldest utterance waiting. Waiting ones older than 20 s never got a
        transcript; drop them."""
        with self._lock:
            while self._pending and time.time() - self._pending[0][1] > 20:
                self._pending.popleft()
            line = self._items.get(item_id)
            if line is not None:
                for entry in list(self._pending):
                    if entry[0] == line:
                        self._pending.remove(entry)
            elif self._pending:
                self._items[item_id] = self._pending.popleft()[0]

    def _line_for(self, item_id):
        line = self._items.get(item_id)
        if line is None:
            if self._line is not None:
                # gpt-live-transcribe sends words WHILE the person talks, before any commit: an
                # unknown item then is the utterance being spoken now.
                self._items[item_id] = line = self._line
            elif self._pending:
                # Otherwise (no commit confirmation seen yet): the oldest utterance waiting.
                self._committed(item_id)
                line = self._items.get(item_id)
        return line

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self.client.realtime.connect(extra_query={"intent": "transcription"}) as conn:
                    conn.session.update(session={"type": "transcription", "audio": {"input": {
                        "format": {"type": "audio/pcm", "rate": RATE},
                        "transcription": _transcription_config(),
                        "turn_detection": None,          # our own voice detector commits utterances
                    }}})
                    backlog, self._backlog = self._backlog, []
                    for chunk in backlog:              # what was said while connecting
                        conn.input_audio_buffer.append(audio=base64.b64encode(chunk).decode())
                    deferred, self._deferred = self._deferred, []
                    if deferred and backlog:
                        # It ended before we were connected: commit it now, as one line.
                        line = deferred[0]
                        self._pending.append((line, time.time()))
                        conn.input_audio_buffer.commit()
                        self._ended.setdefault(line, time.time())
                        threading.Timer(SETTLE_S, self._ready, args=(line,)).start()
                    self._conn = conn
                    backoff = 1.0
                    for ev in conn:
                        if self._stop.is_set():
                            break
                        t = ev.type
                        if t == "input_audio_buffer.committed":
                            self._committed(getattr(ev, "item_id", None))
                        elif t.endswith("input_audio_transcription.delta"):
                            line = self._line_for(ev.item_id)
                            if line is not None:
                                if line in self._recovering:
                                    continue
                                self._text[line] = self._text.get(line, "") + ev.delta
                                self._last_delta[line] = time.time()
                                self._shown[line] = max(self._shown.get(line, 0), len(self._text[line].split()))
                                self.on_partial(line, fix_names(self._text[line].strip()))
                        elif t.endswith("input_audio_transcription.completed"):
                            line = self._line_for(ev.item_id)
                            if line is not None and line not in self._recovering:
                                text = fix_names((ev.transcript or self._text.get(line, "")).strip())
                                shown = self._shown.get(line, 0)
                                if shown >= 4 and len(text.split()) < SHORTER_RATIO * shown and line in self._audio:
                                    self.stats["shortened"] += 1
                                    self._recover_later(line, f"final had {len(text.split())} words, {shown} were shown")
                                    continue
                                self._text[line] = text
                                self.stats["final"] += 1
                                self._forget(line)
                                self.on_final(line, text)
                        elif t == "error":
                            msg = getattr(ev.error, "message", str(ev.error))
                            if "buffer" not in msg.lower():   # committing an empty buffer is harmless
                                self.on_error(msg)
            except Exception as e:
                if self._stop.is_set():
                    break
                if not self._wake.is_set():       # reset() closes it on purpose: not an error
                    print(f"[stream:{self.who}] connection lost ({type(e).__name__}: {e}); reconnecting")
                    traceback.print_exc()
                    self.on_error("Reconnecting…")
            finally:
                lost = self._conn is not None and not self._wake.is_set() and not self._stop.is_set()
                self._conn = None
                with self._lock:
                    # Lines committed on the lost connection will never get a transcript: they are
                    # transcribed again from their kept audio. The one being said now lost its start.
                    self._pending.clear()
                    self._items.clear()
                    waiting = list(self._waiting)
                    if lost and self._line is not None:
                        self._broken.add(self._line)
                if lost:
                    self.stats["reconnects"] += 1
                    for line in waiting:
                        self._recover_later(line, "the connection dropped before its words came")
            if self._wake.wait(backoff):         # a call is starting: reconnect now
                backoff = 0.5
            else:
                backoff = min(backoff * 2, 15)
            self._wake.clear()
