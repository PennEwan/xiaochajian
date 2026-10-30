"""Keep the sync device token in a file only the current Windows user can decrypt (DPAPI).

Same technique as the recharge plugin's vault, with its own entropy: the two plugins
share no code and no secrets.
"""
from __future__ import annotations

import ctypes
import json
from ctypes import wintypes
from pathlib import Path

from .. import store

_ENTROPY = b"codex-task-recorder/sync-device/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(fn_name: str, data: bytes) -> bytes | None:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = getattr(crypt32, fn_name)
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    src, _src_buf = _blob(data)
    entropy, _ent_buf = _blob(_ENTROPY)
    out = _Blob()
    if not fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, _CRYPTPROTECT_UI_FORBIDDEN,
              ctypes.byref(out)):
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save(path: Path, data: dict) -> None:
    sealed = _crypt("CryptProtectData", json.dumps(data).encode("utf-8"))
    if sealed is None:
        raise OSError("CryptProtectData failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(sealed)
    store.replace(tmp, path)


def load(path: Path) -> dict | None:
    try:
        sealed = path.read_bytes()
    except OSError:
        return None
    plain = _crypt("CryptUnprotectData", sealed)
    if plain is None:
        return None
    try:
        value = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def clear(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # missing, or locked by antivirus: a revoked token is rejected anyway
        pass
