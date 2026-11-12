"""Settings > Check audio: is the other side of a meeting reachable on this laptop?"""
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio


def run():
    out = {}

    def work():
        pa = pyaudio.PyAudio()                      # PortAudio is opened and used on this one thread
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            speaker = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            loop = pa.get_default_wasapi_loopback()
            mic = pa.get_device_info_by_index(wasapi["defaultInputDevice"])
            got = {"loop": [], "mic": []}

            def tap(key):
                def cb(data, n, t, s):
                    got[key].append(np.frombuffer(data, dtype=np.int16).astype(np.float32))
                    return (None, pyaudio.paContinue)
                return cb

            def open_in(dev, key):
                return pa.open(format=pyaudio.paInt16, channels=max(1, min(2, int(dev["maxInputChannels"]))),
                               rate=int(dev["defaultSampleRate"]), input=True, input_device_index=dev["index"],
                               frames_per_buffer=int(dev["defaultSampleRate"] * 0.03), stream_callback=tap(key))
            s_loop, s_mic = open_in(loop, "loop"), open_in(mic, "mic")
            rate = int(speaker["defaultSampleRate"])
            t = np.arange(int(rate * 1.5)) / rate
            tone = (0.25 * 32767 * np.sin(2 * np.pi * 523 * t)).astype(np.int16)
            o = pa.open(format=pyaudio.paInt16, channels=2, rate=rate, output=True, output_device_index=speaker["index"])
            time.sleep(0.3)
            o.write(np.repeat(tone, 2).tobytes())
            time.sleep(0.3)
            o.close()
            s_loop.stop_stream()
            s_loop.close()
            s_mic.stop_stream()
            s_mic.close()

            def level(xs):
                return float(np.sqrt(np.mean(np.concatenate(xs) ** 2))) if xs else 0.0
            out.update(speaker=speaker["name"], mic=mic["name"], loop=level(got["loop"]), mic_level=level(got["mic"]))
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
        finally:
            pa.terminate()

    th = threading.Thread(target=work)
    th.start()
    th.join(20)
    if "error" in out:
        return {"ok": False, "text": "Audio check failed: " + out["error"]}
    if not out:
        return {"ok": False, "text": "Audio check did not finish."}
    lines = [f"Speaker: {out['speaker']}", f"Microphone: {out['mic']}"]
    ok = True
    if out["loop"] > 20:
        lines.append("The other side of meetings can be heard: yes.")
    else:
        ok = False
        lines.append("The other side of meetings can be heard: NO. The tone played to the speaker did not reach the recorder. "
                     "Make sure the meeting app plays through this speaker (the Windows default), not another device.")
    if out["mic_level"] < 5:
        ok = False
        lines.append("The microphone is not sending sound.")
    return {"ok": ok, "text": "\n".join(lines)}
