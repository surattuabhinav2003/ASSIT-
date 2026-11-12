"""The OpenAI key, kept on this laptop encrypted for this Windows user (DPAPI): another Windows user, or a copy of
the file on another laptop, cannot read it. Never printed or logged."""
import ctypes
import ctypes.wintypes as wt
import os

from . import paths

KEY_FILE = os.path.join(paths.HOME, "key.bin")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("Windows could not " + ("protect" if protect else "unprotect") + " the key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def save_key(key):
    paths.ensure()
    with open(KEY_FILE, "wb") as f:
        f.write(_dpapi(key.strip().encode("utf-8"), True))


def load_key():
    try:
        with open(KEY_FILE, "rb") as f:
            return _dpapi(f.read(), False).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def forget_key():
    try:
        os.remove(KEY_FILE)
    except OSError:
        pass
