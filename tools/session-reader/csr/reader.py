"""Find rollout files in one user-chosen directory and read complete JSONL lines from an offset.

Read-only by construction: files are opened for reading only and, on Windows, with
FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE so that Codex can keep appending,
renaming or archiving a session while we read it. Handles are closed after every read.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

ROLLOUT_GLOB = "rollout-*.jsonl"
DEFAULT_MAX_BYTES = 8 << 20
_BOM = b"\xef\xbb\xbf"
_SESSION_ID = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.IGNORECASE)


def session_id_from_name(name: str) -> str | None:
    m = _SESSION_ID.search(name)
    return m.group(1).lower() if m else None


def _inside(path: Path, root: Path) -> bool:
    try:
        real = os.path.realpath(path)
        return os.path.commonpath([real, root]) == str(root) and real != str(root)
    except ValueError:  # different drives
        return False


def discover(root: Path, recursive: bool = False, sessions: Iterable[str] = ()) -> list[Path]:
    """rollout-*.jsonl under `root` only; paths that resolve outside it (links, junctions) are skipped."""
    root = Path(root)
    real_root = Path(os.path.realpath(root))
    wanted = [s.lower() for s in sessions]
    found = root.rglob(ROLLOUT_GLOB) if recursive else root.glob(ROLLOUT_GLOB)
    out = []
    for path in found:
        if not path.is_file() or not _inside(path, real_root):
            continue
        if wanted:
            sid = session_id_from_name(path.name) or ""
            if not any(sid.startswith(w) for w in wanted):
                continue
        out.append(path)
    return sorted(out)


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _GENERIC_READ = 0x80000000
    _SHARE_ALL = 0x1 | 0x2 | 0x4  # read | write | delete
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    def open_shared(path: Path) -> BinaryIO:
        handle = _CreateFileW(str(path), _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING,
                              _FILE_ATTRIBUTE_NORMAL, None)
        if handle is None or handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except OSError:
            _CloseHandle(handle)
            raise
        return os.fdopen(fd, "rb")
else:
    def open_shared(path: Path) -> BinaryIO:
        return open(path, "rb")


def head_sha(path: Path, length: int) -> str:
    with open_shared(path) as f:
        return hashlib.sha256(f.read(length)).hexdigest()


@dataclass
class Chunk:
    lines: list[tuple[int, bytes]]  # (byte offset of the line, line without "\n"); blank lines dropped
    end: int  # offset just after the last complete line
    size: int  # file size when read
    eof: bool  # True when everything up to `size` was examined
    pending: int  # bytes of an incomplete last line (only meaningful when eof)


def read_complete_lines(path: Path, offset: int, max_bytes: int = DEFAULT_MAX_BYTES) -> Chunk:
    """Complete lines from `offset`; an incomplete last line is left for the next call.

    Reads about `max_bytes`, but always at least one whole line when one exists.
    """
    with open_shared(path) as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(offset)
        data = f.read(max_bytes)
        while data and b"\n" not in data and offset + len(data) < size:
            more = f.read(max_bytes)
            if not more:
                break
            data += more
    eof = offset + len(data) >= size
    cut = data.rfind(b"\n") + 1
    lines: list[tuple[int, bytes]] = []
    pos = offset
    body = data[:cut]
    if offset == 0 and body.startswith(_BOM):
        body, pos = body[len(_BOM):], len(_BOM)
    for raw in body.split(b"\n")[:-1]:
        if raw.strip():
            lines.append((pos, raw))
        pos += len(raw) + 1
    end = offset + cut
    return Chunk(lines=lines, end=end, size=size, eof=eof, pending=(size - end) if eof else 0)
