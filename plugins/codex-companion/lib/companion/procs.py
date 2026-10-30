"""Find and stop the bundle's own processes: the uploader, the recharge window, the control
panel and the update checker are all python(w).exe from the bundle's runtime — either the
install directory's copy or the copy Codex keeps in its plugin cache.

Win32 directly (EnumProcesses / QueryFullProcessImageNameW / TerminateProcess): no PowerShell,
fast enough to run inside an uninstaller.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
NAMES = {"python.exe", "pythonw.exe"}


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _api():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, k32, psapi


def _pids() -> list[int]:
    ctypes, wintypes, _, psapi = _api()
    size = 4096
    while True:
        arr = (wintypes.DWORD * size)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
            return []
        count = needed.value // ctypes.sizeof(wintypes.DWORD)
        if count < size:
            return [arr[i] for i in range(count) if arr[i]]
        size *= 2


def image_path(pid: int) -> str | None:
    ctypes, wintypes, k32, _ = _api()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(handle)


def find(roots: list[Path], exclude: set[int] | None = None, names=NAMES) -> list[tuple[int, str]]:
    """(pid, image) of python/pythonw processes whose executable lives under one of `roots`."""
    if os.name != "nt":
        return []
    exclude = (exclude or set()) | {os.getpid()}
    prefixes = [_norm(r).rstrip("\\/") + os.sep for r in roots if r]
    found = []
    for pid in _pids():
        if pid in exclude:
            continue
        image = image_path(pid)
        if not image or Path(image).name.lower() not in names:
            continue
        if any(_norm(image).startswith(p) for p in prefixes):
            found.append((pid, image))
    return found


def terminate(pid: int, wait_s: float = 3.0) -> bool:
    _, _, k32, _ = _api()
    handle = k32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        ok = bool(k32.TerminateProcess(handle, 1))
        k32.WaitForSingleObject(handle, int(wait_s * 1000))
        return ok
    finally:
        k32.CloseHandle(handle)


def stop(roots: list[Path], exclude: set[int] | None = None, settle_s: float = 0.3, names=NAMES) -> list[str]:
    """Terminate every bundle process under `roots`; returns their image names."""
    stopped = []
    for pid, image in find(roots, exclude, names):
        if terminate(pid):
            stopped.append(Path(image).name)
    if stopped:
        time.sleep(settle_s)  # let Windows release the image files before they are replaced
    return stopped
