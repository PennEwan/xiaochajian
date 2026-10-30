"""Per-session JSONL files and cursors, written under a per-session file lock.

Several hook processes can run at once (e.g. PostToolUse and Stop), so every
append and cursor update for one session happens inside the same lock.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import timeutil
from .paths import Paths, safe_name

LOCK_TIMEOUT_S = 2.0

if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class LockTimeout(Exception):
    pass


class SessionHandle:
    def __init__(self, paths: Paths, session_id: str, cursor: dict):
        self.paths = paths
        self.session_id = session_id
        self.cursor = cursor

    @property
    def events_file(self) -> Path:
        return self.paths.sessions / self.cursor["day"] / f"{safe_name(self.session_id)}.jsonl"

    def append(self, events: list[dict]) -> None:
        if not events:
            return
        path = self.events_file
        path.parent.mkdir(parents=True, exist_ok=True)
        seq = self.cursor.get("next_seq") or self.cursor.get("event_count", 0) + 1
        for e in events:
            e["seq"] = seq
            seq += 1
        self.cursor["next_seq"] = seq
        payload = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        with open(path, "ab") as f:
            f.write(payload.encode("utf-8"))
        self.cursor["event_count"] = self.cursor.get("event_count", 0) + len(events)
        self.cursor["last_event_at"] = events[-1]["recorded_at"]


def replace(src: Path, dst: Path, attempts: int = 50, wait_s: float = 0.02) -> None:
    """os.replace that rides out a reader: Windows refuses to replace a file while another
    process has it open (the uploader and `ctr status` read cursor and state files), and
    such readers let go within milliseconds."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait_s)


@contextmanager
def locked(lock_path: Path, timeout: float = LOCK_TIMEOUT_S, label: str = "") -> Iterator[None]:
    """Hold an exclusive lock on `lock_path` (created if needed) for the duration of the block."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise LockTimeout(label or lock_path.name)
            time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def try_hold(lock_path: Path) -> int | None:
    """Take a lock for as long as the process wants it (single-instance guard): an fd, or None."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if _try_lock(fd):
        return fd
    os.close(fd)
    return None


def release(fd: int) -> None:
    try:
        _unlock(fd)
    finally:
        os.close(fd)


def is_held(lock_path: Path) -> bool:
    """True while some process holds `lock_path`."""
    if not lock_path.exists():
        return False
    fd = try_hold(lock_path)
    if fd is None:
        return True
    release(fd)
    return False


@contextmanager
def session(paths: Paths, session_id: str) -> Iterator[SessionHandle]:
    name = safe_name(session_id)
    cursor_path = paths.cursors / f"{name}.json"
    with locked(paths.cursors / f"{name}.lock", label=session_id):
        try:
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            cursor = {"session_id": session_id, "day": timeutil.now().strftime("%Y-%m-%d"),
                      "transcripts": {}}
        handle = SessionHandle(paths, session_id, cursor)
        try:
            yield handle
        finally:
            tmp = cursor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(handle.cursor, ensure_ascii=False), encoding="utf-8")
            replace(tmp, cursor_path)


def iter_events(paths: Paths) -> Iterator[dict]:
    """Every recorded event, file by file (used by status and report)."""
    if not paths.sessions.exists():
        return
    for file in sorted(paths.sessions.glob("*/*.jsonl")):
        with open(file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
