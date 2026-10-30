"""Small Win32 helpers for driving and observing the real Codex desktop App in acceptance tests.

Only standard library (ctypes). Window lookup is by owning process (ChatGPT.exe = the Codex App)
or by exact title; input goes to a window only after it is verified to be in the foreground, so
keystrokes can never land in some other application.
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

U = ctypes.WinDLL("user32", use_last_error=True)
K = ctypes.WinDLL("kernel32", use_last_error=True)
ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
U.SendMessageW.restype = ctypes.c_ssize_t
U.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p]


def text(h) -> str:
    n = U.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    U.GetWindowTextW(h, b, n + 1)
    return b.value


def cls(h) -> str:
    b = ctypes.create_unicode_buffer(256)
    U.GetClassNameW(h, b, 256)
    return b.value


def owner(h) -> tuple[int, str]:
    pid = wintypes.DWORD()
    U.GetWindowThreadProcessId(h, ctypes.byref(pid))
    hp = K.OpenProcess(0x1000, False, pid.value)
    b = ctypes.create_unicode_buffer(512)
    n = wintypes.DWORD(512)
    if hp:
        K.QueryFullProcessImageNameW(hp, 0, b, ctypes.byref(n))
        K.CloseHandle(hp)
    return pid.value, b.value.rsplit("\\", 1)[-1]


def rect(h) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    U.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def windows(process: str | None = None, title: str | None = None, visible_only: bool = True) -> list[dict]:
    out = []

    def cb(h, _):
        if visible_only and not U.IsWindowVisible(h):
            return True
        t = text(h)
        pid, name = owner(h)
        if process and name.lower() != process.lower():
            return True
        if title is not None and t != title:
            return True
        out.append({"hwnd": h, "pid": pid, "process": name, "title": t, "class": cls(h),
                    "iconic": bool(U.IsIconic(h)), "rect": rect(h)})
        return True

    U.EnumWindows(ENUM(cb), 0)
    return out


def app_window() -> dict | None:
    """The Codex App's main window (largest visible ChatGPT.exe top-level window with a title)."""
    ws = [w for w in windows("ChatGPT.exe") if w["title"]]
    if not ws:
        return None
    return max(ws, key=lambda w: (w["rect"][2] - w["rect"][0]) * (w["rect"][3] - w["rect"][1]))


def foreground() -> int:
    return U.GetForegroundWindow()


def bring_to_front(h) -> bool:
    """Restore and focus a window. Uses the documented ALT-key trick so SetForegroundWindow is honoured."""
    SW_RESTORE, SW_SHOW = 9, 5
    if U.GetForegroundWindow() == h and not U.IsIconic(h):
        return True  # already in front: pressing ALT would close any open menu or dropdown
    if U.IsIconic(h):
        U.ShowWindow(h, SW_RESTORE)
    else:
        U.ShowWindow(h, SW_SHOW)
    for _ in range(5):
        U.keybd_event(0x12, 0, 0, 0)          # ALT down: lets this process take the foreground
        U.SetForegroundWindow(h)
        U.keybd_event(0x12, 0, 2, 0)          # ALT up
        time.sleep(0.3)
        if U.GetForegroundWindow() == h:
            return True
    return False


def screenshot(path: Path, region: tuple[int, int, int, int] | None = None) -> Path:
    if region:
        x, y, r, b = region
        w, h = r - x, b - y
    else:
        x, y, w, h = 0, 0, U.GetSystemMetrics(0), U.GetSystemMetrics(1)
    ps = (f"Add-Type -AssemblyName System.Drawing; $b = New-Object System.Drawing.Bitmap {w},{h}; "
          f"$g = [System.Drawing.Graphics]::FromImage($b); $g.CopyFromScreen({x},{y},0,0,$b.Size); "
          f"$b.Save('{path}'); $g.Dispose(); $b.Dispose()")
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True)
    return path


# ---- input (only ever sent while the expected window is in the foreground) ----------------------

class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        class KI(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MI(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        _fields_ = [("ki", KI), ("mi", MI)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    U.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    i = INPUT()
    i.type = 1
    i.ki.wVk, i.ki.wScan, i.ki.dwFlags = vk, scan, flags
    return i


class NotForeground(RuntimeError):
    pass


def _guard(h) -> None:
    if U.GetForegroundWindow() != h:
        raise NotForeground(f"expected window {h} in the foreground, got {U.GetForegroundWindow()}")


def type_text(h, s: str) -> None:
    _guard(h)
    KEYEVENTF_UNICODE, KEYUP = 0x4, 0x2
    for ch in s:
        _guard(h)
        _send([_key(0, ord(ch), KEYEVENTF_UNICODE), _key(0, ord(ch), KEYEVENTF_UNICODE | KEYUP)])
        time.sleep(0.01)


def press(h, *vks: int) -> None:
    """Chord: press all, release in reverse (e.g. press(h, 0x11, 0x4E) = Ctrl+N)."""
    _guard(h)
    _send([_key(vk) for vk in vks] + [_key(vk, 0, 2) for vk in reversed(vks)])
    time.sleep(0.1)


def click(h, x: int, y: int) -> None:
    """Left click at screen coordinates (must lie inside window h, which must be in the foreground)."""
    _guard(h)
    l, t, r, b = rect(h)
    if not (l <= x < r and t <= y < b):
        raise ValueError(f"({x},{y}) outside window {rect(h)}")
    sw, sh = U.GetSystemMetrics(0), U.GetSystemMetrics(1)
    i = INPUT()
    i.type = 0
    i.mi.dx, i.mi.dy = int(x * 65535 / (sw - 1)), int(y * 65535 / (sh - 1))
    i.mi.dwFlags = 0x8000 | 0x0001  # ABSOLUTE | MOVE
    down, up = INPUT(), INPUT()
    down.type = up.type = 0
    down.mi.dwFlags, up.mi.dwFlags = 0x0002, 0x0004
    _send([i])
    time.sleep(0.05)
    _guard(h)
    _send([down, up])
    time.sleep(0.2)


def hover(h, x: int, y: int) -> None:
    """Move the mouse over (x, y) inside window h without clicking."""
    _guard(h)
    sw, sh = U.GetSystemMetrics(0), U.GetSystemMetrics(1)
    i = INPUT()
    i.type = 0
    i.mi.dx, i.mi.dy = int(x * 65535 / (sw - 1)), int(y * 65535 / (sh - 1))
    i.mi.dwFlags = 0x8000 | 0x0001
    _send([i])
    time.sleep(0.3)


VK_RETURN, VK_CONTROL, VK_ESCAPE, VK_SHIFT, VK_TAB = 0x0D, 0x11, 0x1B, 0x10, 0x09
