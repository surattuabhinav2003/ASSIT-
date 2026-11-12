"""Your own voice sample, kept on this laptop (%LOCALAPPDATA%\CFMinutes\me.wav, about 20 seconds).

With it the after-meeting pass recognises YOU on the microphone every time, so a colleague sitting next to you is
a different person instead of being merged into you. It is made by itself from the first meeting where the
microphone heard exactly one voice, and only from stretches where nobody else was speaking. Settings can delete it."""
import os
import wave

import numpy as np

from . import paths

RATE = 16000
PATH = os.path.join(paths.HOME, "me.wav")
MIN_S, MAX_S = 12.0, 25.0


def exists():
    return os.path.isfile(PATH)


def load():
    """The sample as a data URL for the speaker-separation call, or None."""
    import base64
    try:
        with open(PATH, "rb") as f:
            return "data:audio/wav;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return None


def forget():
    try:
        os.remove(PATH)
    except OSError:
        pass


def save(x):
    paths.ensure()
    with wave.open(PATH, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.asarray(x, dtype=np.int16).tobytes())


def maybe_enrol(track, segments, remote_segments):
    """track: the microphone audio (16 kHz int16); segments: [(t0, t1)] where the one microphone voice spoke;
    remote_segments: [(t0, t1)] where anyone on the other side spoke. Saves a sample when none exists and there is
    enough clean speech. Returns True when it saved one."""
    if exists():
        return False
    clean = []
    for a, b in sorted(segments, key=lambda s: s[0] - s[1]):          # longest first
        if b - a < 2.0:
            continue
        if any(min(b, rb) - max(a, ra) > 0.3 for ra, rb in remote_segments):
            continue                                                  # the other side was talking: could be an echo
        clean.append((a, b))
    total, parts = 0.0, []
    for a, b in clean:
        take = min(b - a, MAX_S - total)
        parts.append(track[int(a * RATE):int((a + take) * RATE)])
        total += take
        if total >= MAX_S:
            break
    if total < MIN_S:
        return False
    save(np.concatenate(parts))
    return True
