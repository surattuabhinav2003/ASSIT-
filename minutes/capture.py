"""Hears both sides of a meeting and saves each side's audio.

  others  what this laptop PLAYS (WASAPI loopback): Teams, Zoom, a browser, any app, everyone but you
  you     the default microphone

Each side has its own voice detector (Silero), its own live transcription (stream.py, words appear while
people are still talking) and its own audio file on the same clock, so the two files line up. The audio
files are what the after-meeting pass (finish.py) transcribes again with speaker separation.

The voice detector and automatic gain follow CloudFuze Assist's audio.py (copied, not imported: this is a
separate app)."""
import collections
import os
import queue
import threading
import time
import wave

import numpy as np
import pyaudiowpatch as pyaudio

os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")        # the detector's helper threads otherwise spin the CPU

RATE = 24000          # what the live transcription takes
FILE_RATE = 16000     # what is saved: 1.9 MB a minute per side
FRAME_MS = 30
SPEECH_ON, SPEECH_OFF = 0.5, 0.35
TARGET_RMS, MAX_GAIN = 3000.0, 30.0


def _silero():
    try:
        from pysilero_vad import SileroVoiceActivityDetector
        return SileroVoiceActivityDetector()
    except Exception as e:
        print(f"[capture] Silero VAD unavailable ({type(e).__name__}: {e}); using the loudness rule")
        return None


class TrackWriter:
    """A 16 kHz mono WAV on the meeting's clock: a silent stretch (the speakers play nothing, so Windows
    sends nothing) is written as zeros, so a position in the file is a time in the meeting."""

    def __init__(self, path, t0):
        self.t0, self.written = t0, 0
        self._w = wave.open(path, "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(FILE_RATE)
        self._lock = threading.Lock()
        self._patched = time.time()

    def add(self, x24, now):
        if not len(x24):
            return
        n = max(1, int(len(x24) * FILE_RATE / RATE))
        x = np.interp(np.linspace(0, len(x24) - 1, n), np.arange(len(x24)), x24)
        pcm = np.clip(x, -32768, 32767).astype(np.int16)
        with self._lock:
            start = int((now - self.t0) * FILE_RATE) - n            # where this piece should begin
            gap = start - self.written
            if gap > FILE_RATE // 10:
                self._w.writeframes(np.zeros(gap, dtype=np.int16).tobytes())
                self.written += gap
            self._w.writeframes(pcm.tobytes())
            self.written += n
            if time.time() - self._patched > 10:
                # keep the file's length in its header current: a crash then leaves a playable recording
                self._patched = time.time()
                try:
                    self._w._patchheader()
                    self._w._file.flush()
                except Exception:
                    pass

    def close(self):
        with self._lock:
            try:
                self._w.close()
            except Exception:
                pass


class Listener:
    def __init__(self, pa, device, sink=None, tap=None, keep=None, on_level=None,
                 silence_ms=700, min_speech_ms=350, max_utt_s=25, agc=False):
        self.sink, self.tap = sink, tap
        self.keep = keep or (lambda t0, t1: True)
        self.on_level = on_level
        self.silence_ms, self.min_speech_ms, self.max_utt_s = silence_ms, min_speech_ms, max_utt_s
        self.device_name = device["name"]
        self.rate = int(device["defaultSampleRate"])
        self.channels = max(1, min(2, int(device["maxInputChannels"])))
        self.speech_spans = []
        self.vad = _silero()
        self.agc = agc
        self._gain, self._voice_rms = 1.0, None
        self._vad_buf = np.zeros(0, dtype=np.int16)
        self._prob = 0.0
        self._frames = queue.Queue()
        self._stop = threading.Event()
        self.last_audio = 0.0                 # when Windows last delivered a frame (a dead device stops)

        def callback(in_data, frame_count, time_info, status):
            self.last_audio = time.time()
            self._frames.put((self.last_audio, in_data))
            return (None, pyaudio.paContinue)

        self._stream = pa.open(format=pyaudio.paInt16, channels=self.channels, rate=self.rate, input=True,
                               input_device_index=device["index"],
                               frames_per_buffer=int(self.rate * FRAME_MS / 1000), stream_callback=callback)
        threading.Thread(target=self._loop, daemon=True, name=f"listen-{device['name'][:12]}").start()

    def stop(self):
        self._stop.set()
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass

    def speaking_during(self, t0, t1):
        if t1 <= t0:
            return 0.0
        return sum(max(0.0, min(e, t1) - max(s, t0)) for s, e in list(self.speech_spans)) / (t1 - t0)

    def _to_mono(self, raw):
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if self.channels > 1:
            x = x.reshape(-1, self.channels).mean(axis=1)
        if self.rate != RATE and len(x):
            n = int(len(x) * RATE / self.rate)
            x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        return x

    def _speech_prob(self, x):
        if not len(x):
            return self._prob
        x16 = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / RATE)), np.arange(len(x)), x)
        self._vad_buf = np.concatenate([self._vad_buf, np.clip(x16, -32768, 32767).astype(np.int16)])
        n = self.vad.chunk_samples()
        while len(self._vad_buf) >= n:
            chunk, self._vad_buf = self._vad_buf[:n], self._vad_buf[n:]
            try:
                self._prob = float(self.vad(chunk.tobytes()))
            except Exception:
                self._prob = 0.0
        return self._prob

    def _apply_gain(self, x, speaking, rms):
        if speaking and rms > 30:
            self._voice_rms = rms if self._voice_rms is None else 0.97 * self._voice_rms + 0.03 * rms
            want = min(MAX_GAIN, max(1.0, TARGET_RMS / self._voice_rms))
            self._gain += (want - self._gain) * 0.1
        return x * self._gain

    def _loop(self):
        noise = 200.0
        speech_ms, in_speech, silent_ms, t_start = 0, False, 0, 0.0
        loud_since = None
        while not self._stop.is_set():
            try:
                now, raw = self._frames.get(timeout=0.1)
                x = self._to_mono(raw)
                ms = len(x) * 1000 / RATE
            except queue.Empty:
                now, x, ms = time.time(), np.zeros(int(RATE * 0.1), dtype=np.float32), 100   # loopback: nothing playing = silence
            rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
            if self.on_level:
                self.on_level(min(1.0, rms * self._gain / 3000))
            if self.tap:
                self.tap(x, now)                                   # the saved audio is not boosted
            if self.vad is not None:
                loud = self._speech_prob(x) >= (SPEECH_OFF if in_speech or loud_since is not None else SPEECH_ON)
            else:
                loud = rms > max(350.0, noise * 3)
                if not loud:
                    noise = 0.95 * noise + 0.05 * rms
            if loud and loud_since is None:
                loud_since = now
            elif not loud and loud_since is not None:
                self.speech_spans.append((loud_since, now))
                loud_since = None
                self.speech_spans = [s for s in self.speech_spans if s[1] > now - 60]
            if self.agc:
                x = self._apply_gain(x, loud, rms)
            pcm = np.clip(x, -32768, 32767).astype(np.int16)
            if loud and not in_speech:
                in_speech, t_start = True, now
                if self.sink:
                    self.sink.start()
            if self.sink:
                self.sink.audio(pcm.tobytes())
            if loud:
                silent_ms, speech_ms = 0, speech_ms + ms
            elif in_speech:
                silent_ms += ms
                if silent_ms >= self.silence_ms:
                    self._finish(speech_ms, t_start, now)
                    speech_ms, in_speech, silent_ms = 0, False, 0
            if in_speech and speech_ms / 1000 >= self.max_utt_s:
                self._finish(speech_ms, t_start, now)
                speech_ms, silent_ms, t_start = 0, 0, now
                if self.sink:
                    self.sink.start()                              # still talking: carry on in a new line

    def _finish(self, speech_ms, t0, t1):
        keep = speech_ms >= self.min_speech_ms and self.keep(t0, t1)
        if self.sink:
            self.sink.end(keep=keep)


class Capture:
    """Both sides at once. events(kind, side, line_id, text, t) is called for every change:
         kind "start"  a person began speaking            (side, line)
              "text"   the line so far                    (side, line, text)
              "final"  the finished line                  (side, line, text)
              "end"    they stopped; t = (t0, t1)         (side, line, None, (t0, t1)); text None
              "error"  a connection message               (side, None, message)
    Times are seconds from start()."""

    def __init__(self, client, events, on_level=None):
        from .stream import StreamSide
        self.events = events
        self.on_level = on_level or (lambda side, v: None)
        self.pa = pyaudio.PyAudio()
        self.t0 = None
        self._line_start = {}
        self.writers = {}
        self.sides = {}
        self.listeners = {}
        self._client = client
        self._StreamSide = StreamSide
        self.max_level = {"them": 0.0, "you": 0.0}

    def _side(self, who):
        def started(line):
            self._line_start[(who, line)] = time.time() - self.t0
            self.events("start", who, line, None, None)

        def ended(line, kept):
            t0 = self._line_start.pop((who, line), None)
            if t0 is not None and kept:
                self.events("end", who, line, None, (t0, time.time() - self.t0))
            elif not kept:
                self.events("drop", who, line, None, None)

        return self._StreamSide(
            self._client, who,
            on_partial=lambda line, text: self.events("text", who, line, text, None),
            on_ready=lambda line, text, t: None,
            on_final=lambda line, text: self.events("final", who, line, text, None),
            on_error=lambda msg: self.events("error", who, None, msg, None),
            on_start=started, on_end=ended)

    def _loopback(self):
        """The loopback of the default speaker. Windows sometimes cannot name the default output for a moment (a dock or
        headset connecting): ask again with a fresh device list, then take any speaker loopback there is."""
        last = None
        for attempt in range(3):
            try:
                return self.pa.get_default_wasapi_loopback()
            except Exception as e:
                last = e
                try:
                    found = list(self.pa.get_loopback_device_info_generator())
                    if found:
                        print(f"[capture] default speaker not available ({e}); using {found[0]['name']}")
                        return found[0]
                except Exception as e2:
                    last = e2
                time.sleep(1.0)
                try:
                    self.pa.terminate()
                except Exception:
                    pass
                self.pa = pyaudio.PyAudio()                          # re-reads the device list
        raise RuntimeError("No speaker output was found to record the other side. Check the sound output in Windows "
                           "(Settings, System, Sound) and try again.") from last

    def start(self, others_path=None, you_path=None):
        self.t0 = time.time()
        loop_dev = self._loopback()
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        if others_path:
            self.writers["others"] = TrackWriter(others_path, self.t0)
        if you_path:
            self.writers["you"] = TrackWriter(you_path, self.t0)
        self.sides = {"them": self._side("them"), "you": self._side("you")}
        self.listeners["them"] = Listener(
            self.pa, loop_dev, sink=self.sides["them"], agc=True, silence_ms=700,
            tap=(lambda x, now: self.writers["others"].add(x, now)) if "others" in self.writers else None,
            on_level=lambda v: self._heard("them", v))
        try:
            mic = self.pa.get_device_info_by_index(wasapi["defaultInputDevice"])
            self.listeners["you"] = Listener(
                self.pa, mic, sink=self.sides["you"], silence_ms=600,
                tap=(lambda x, now: self.writers["you"].add(x, now)) if "you" in self.writers else None,
                # with speakers (no headset) the mic also hears the other side: that is not you
                keep=lambda a, b: self.listeners["them"].speaking_during(a, b) <= 0.5,
                on_level=lambda v: self._heard("you", v))
        except Exception as e:
            print(f"[capture] no microphone: {e}")
            self.events("error", "you", None, "No microphone found: only the other side is recorded.", None)

    def _heard(self, side, v):
        self.max_level[side] = max(self.max_level.get(side, 0.0), v)
        self.on_level(side, v)

    def devices(self):
        return {side: l.device_name for side, l in self.listeners.items()}

    def stale(self, seconds=8):
        """Sides whose device has stopped delivering audio (the loopback legitimately goes quiet when nothing plays,
        so only the microphone is judged)."""
        l = self.listeners.get("you")
        return bool(l and l.last_audio and time.time() - l.last_audio > seconds)

    def stop(self):
        for l in self.listeners.values():
            l.stop()
        time.sleep(0.4)
        for s in self.sides.values():
            try:
                s.stop()
            except Exception:
                pass
        for w in self.writers.values():
            w.close()
        try:
            self.pa.terminate()
        except Exception:
            pass
