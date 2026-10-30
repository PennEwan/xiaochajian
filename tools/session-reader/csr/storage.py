"""SQLite store for read offsets, parser context and normalised events.

A batch of events, issues and skip counters is committed in the same transaction as the
new read offset, so an interrupted run never loses or duplicates anything: the next run
resumes from the last committed offset, and `event_key` is a primary key on top of that.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .parser import Issue

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, session_id TEXT NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, head_len INTEGER NOT NULL, head_sha TEXT NOT NULL,
    pending INTEGER NOT NULL, ctx TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    event_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, file TEXT NOT NULL, offset INTEGER NOT NULL,
    idx INTEGER NOT NULL, turn_id TEXT, ordinal INTEGER, kind TEXT NOT NULL, model TEXT, call_id TEXT,
    body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_by_session ON events(session_id, file, offset, idx);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT NOT NULL, session_id TEXT NOT NULL,
    category TEXT NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL, offset INTEGER, ordinal INTEGER,
    count INTEGER NOT NULL, at TEXT NOT NULL, UNIQUE(file, offset, code));
CREATE TABLE IF NOT EXISTS skipped (
    file TEXT NOT NULL, session_id TEXT NOT NULL, category TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY(file, category));
CREATE TABLE IF NOT EXISTS exports (
    path TEXT PRIMARY KEY, created_at TEXT NOT NULL, sessions TEXT NOT NULL, events INTEGER NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class FileState:
    path: str
    session_id: str
    offset: int  # just after the last complete line that was processed
    size: int  # file size when last read
    mtime_ns: int
    head_len: int  # bytes covered by head_sha
    head_sha: str
    pending: int  # bytes of an incomplete last line waiting to be completed
    ctx_json: str
    updated_at: str = ""


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_DDL)
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    # --- read progress -------------------------------------------------------------------------

    def file_state(self, path: str) -> FileState | None:
        row = self.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        if row is None:
            return None
        return FileState(row["path"], row["session_id"], row["offset"], row["size"], row["mtime_ns"],
                         row["head_len"], row["head_sha"], row["pending"], row["ctx"], row["updated_at"])

    def files(self, session_id: str | None = None) -> list[FileState]:
        sql, args = "SELECT path FROM files", ()
        if session_id:
            sql, args = sql + " WHERE session_id=?", (session_id,)
        return [self.file_state(r["path"]) for r in self.conn.execute(sql + " ORDER BY path", args)]

    def commit_batch(self, path: str, expected_offset: int, state: FileState, events: list[dict],
                     issues: list[Issue], skipped: Counter) -> int | None:
        """Store one batch and the new offset atomically.

        Returns how many events were new, or None (nothing stored) if another run moved the offset.
        """
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT offset FROM files WHERE path=?", (path,)).fetchone()
            if (row["offset"] if row else 0) != expected_offset:
                c.execute("ROLLBACK")
                return None
            inserted = c.executemany(
                "INSERT OR IGNORE INTO events(event_key, session_id, file, offset, idx, turn_id, ordinal,"
                " kind, model, call_id, body) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(e["event_key"], e["session_id"], path, e["source"]["offset"],
                  int(e["event_key"].rsplit(":", 1)[1]), e["turn_id"], e["ordinal"], e["kind"], e["model"],
                  e["call_id"], json.dumps(e, ensure_ascii=False)) for e in events]).rowcount
            self._add_issues(path, state.session_id, issues)
            self._add_skipped(path, state.session_id, skipped)
            c.execute(
                "INSERT INTO files(path, session_id, offset, size, mtime_ns, head_len, head_sha, pending, ctx,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET"
                " session_id=excluded.session_id, offset=excluded.offset, size=excluded.size,"
                " mtime_ns=excluded.mtime_ns, head_len=excluded.head_len, head_sha=excluded.head_sha,"
                " pending=excluded.pending, ctx=excluded.ctx, updated_at=excluded.updated_at",
                (path, state.session_id, state.offset, state.size, state.mtime_ns, state.head_len,
                 state.head_sha, state.pending, state.ctx_json, _now()))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return max(inserted, 0)

    def _add_issues(self, path: str, session_id: str, issues: Iterable[Issue]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO issues(file, session_id, category, code, detail, offset, ordinal, count, at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(path, session_id, i.category, i.code, i.detail, i.offset, i.ordinal, i.count, _now())
             for i in issues])

    def _add_skipped(self, path: str, session_id: str, skipped: Counter) -> None:
        self.conn.executemany(
            "INSERT INTO skipped(file, session_id, category, count) VALUES (?,?,?,?)"
            " ON CONFLICT(file, category) DO UPDATE SET count=count+excluded.count,"
            " session_id=excluded.session_id",
            [(path, session_id, k, n) for k, n in skipped.items() if n])

    def add_issue(self, path: str, session_id: str, issue: Issue) -> None:
        self._add_issues(path, session_id, [issue])

    def reset_file(self, path: str) -> None:
        """Forget everything derived from `path` (used when the file was replaced)."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            for table, col in (("events", "file"), ("issues", "file"), ("skipped", "file"), ("files", "path")):
                c.execute(f"DELETE FROM {table} WHERE {col}=?", (path,))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # --- queries -------------------------------------------------------------------------------

    def sessions(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT session_id FROM files UNION SELECT session_id FROM events ORDER BY 1")
        return [r[0] for r in rows]

    def iter_events(self, session_ids: Iterable[str] | None = None) -> Iterator[dict]:
        sql, args = "SELECT body FROM events", []
        ids = list(session_ids or [])
        if ids:
            sql += f" WHERE session_id IN ({','.join('?' * len(ids))})"
            args = ids
        for row in self.conn.execute(sql + " ORDER BY session_id, file, offset, idx", args):
            yield json.loads(row["body"])

    def issues(self, session_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM issues WHERE session_id=? ORDER BY id", (session_id,))
        return [dict(r) for r in rows]

    def skipped(self, session_id: str) -> Counter:
        rows = self.conn.execute("SELECT category, SUM(count) FROM skipped WHERE session_id=? GROUP BY 1",
                                 (session_id,))
        return Counter({r[0]: r[1] for r in rows})

    # --- exports and deletion ------------------------------------------------------------------

    def record_export(self, path: Path, sessions: list[str], events: int) -> None:
        self.conn.execute(
            "INSERT INTO exports(path, created_at, sessions, events) VALUES (?,?,?,?) ON CONFLICT(path)"
            " DO UPDATE SET created_at=excluded.created_at, sessions=excluded.sessions, events=excluded.events",
            (str(path), _now(), json.dumps(sessions), events))

    def exports(self) -> list[dict]:
        return [dict(r, sessions=json.loads(r["sessions"]))
                for r in self.conn.execute("SELECT * FROM exports ORDER BY created_at")]

    def purge_session(self, session_id: str) -> list[str]:
        """Delete a session's rows and every export that contains it; returns removed export paths."""
        removed = []
        for exp in self.exports():
            if session_id in exp["sessions"]:
                if os.path.exists(exp["path"]):
                    os.remove(exp["path"])
                    removed.append(exp["path"])
                self.conn.execute("DELETE FROM exports WHERE path=?", (exp["path"],))
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            for table in ("events", "issues", "skipped", "files"):
                c.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return removed
