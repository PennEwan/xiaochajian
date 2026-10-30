"""One scan pass: find files, detect replacement, read new lines, parse, commit in batches."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .parser import Context, Issue, parse_line
from .reader import DEFAULT_MAX_BYTES, discover, head_sha, read_complete_lines, session_id_from_name
from .storage import FileState, Storage

HEAD_BYTES = 4096
DEFAULT_BATCH_LINES = 2000


@dataclass
class FileResult:
    path: str
    session_id: str
    status: str  # unchanged | read | rewritten | conflict | error
    new_events: int = 0
    new_issues: int = 0
    pending: int = 0
    error: str | None = None


def scan(storage: Storage, root: Path, *, recursive: bool = False, sessions: Iterable[str] = (),
         max_bytes: int = DEFAULT_MAX_BYTES, batch_lines: int = DEFAULT_BATCH_LINES) -> list[FileResult]:
    return [ingest_file(storage, p, max_bytes=max_bytes, batch_lines=batch_lines)
            for p in discover(root, recursive=recursive, sessions=sessions)]


def _fallback_session_id(path: Path) -> str:
    return session_id_from_name(path.name) or "unknown-" + hashlib.sha256(path.name.encode()).hexdigest()[:12]


def ingest_file(storage: Storage, path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                batch_lines: int = DEFAULT_BATCH_LINES) -> FileResult:
    path = Path(path)
    key = str(path)
    state = storage.file_state(key)
    sid = state.session_id if state else _fallback_session_id(path)
    try:
        st = os.stat(path)
        if state and state.size == st.st_size and state.mtime_ns == st.st_mtime_ns:
            return FileResult(key, sid, "unchanged", pending=state.pending)
        status = "read"
        if state and _replaced(path, state, st.st_size):
            storage.reset_file(key)
            storage.add_issue(key, sid, Issue("file", "file_rewritten",
                                              "文件变短或开头内容改变，已删除旧结果并从头重读", None))
            state, status = None, "rewritten"
        return _read_new(storage, path, state, st, status, max_bytes, batch_lines)
    except (OSError, sqlite3.Error) as exc:
        detail = f"{type(exc).__name__}: {getattr(exc, 'strerror', None) or exc}"
        if isinstance(exc, sqlite3.Error):
            code, detail = "store_failed", f"写入数据库失败，本批已回滚，下次从上次提交处重读（{detail}）"
        else:
            code, detail = "file_unreadable", f"读取会话文件失败（{detail}）"
        try:
            storage.add_issue(key, sid, Issue("file", code, detail[:300], state.offset if state else 0))
        except sqlite3.Error:
            pass
        return FileResult(key, sid, "error", error=detail)


def _replaced(path: Path, state: FileState, size: int) -> bool:
    if size < state.offset or size < state.head_len:
        return True
    return bool(state.head_len) and head_sha(path, state.head_len) != state.head_sha


def _read_new(storage: Storage, path: Path, state: FileState | None, st: os.stat_result, status: str,
              max_bytes: int, batch_lines: int) -> FileResult:
    key = str(path)
    if state is None:
        ctx = Context(session_id=_fallback_session_id(path))
        offset, head_len, head = 0, 0, ""
    else:
        ctx = Context.from_json(state.ctx_json)
        offset, head_len, head = state.offset, state.head_len, state.head_sha
    if head_len < HEAD_BYTES and st.st_size > head_len:  # first read, or the file was tiny last time
        head_len = min(HEAD_BYTES, st.st_size)
        head = head_sha(path, head_len)

    result = FileResult(key, ctx.session_id, status)
    while True:
        start = offset
        chunk = read_complete_lines(path, offset, max_bytes)
        batches = [chunk.lines[i:i + batch_lines] for i in range(0, len(chunk.lines), batch_lines)] or [[]]
        for n, batch in enumerate(batches):
            last = n == len(batches) - 1
            end = chunk.end if last else batch[-1][0] + len(batch[-1][1]) + 1
            events, issues, skipped = [], [], Counter()
            for line_offset, raw in batch:
                parsed = parse_line(raw, line_offset, ctx, path.name)
                events += parsed.events
                issues += parsed.issues
                skipped.update(parsed.skipped)
            new_state = FileState(key, ctx.session_id, end, chunk.size, st.st_mtime_ns, head_len, head,
                                  chunk.pending if last and chunk.eof else 0, ctx.to_json())
            inserted = storage.commit_batch(key, offset, new_state, events, issues, skipped)
            if inserted is None:
                result.status = "conflict"
                return result
            result.new_events += inserted
            result.new_issues += len(issues)
            offset = end
        result.session_id = ctx.session_id
        result.pending = chunk.pending if chunk.eof else 0
        if chunk.eof or offset == start:  # the second test only guards against a reader without progress
            return result
