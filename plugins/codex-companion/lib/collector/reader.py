"""Read new events from the rollout files Codex writes itself.

This is the whole point of separating the collector from the plugin: `%CODEX_HOME%\\sessions\\**\\
rollout-*.jsonl` is written by Codex whether or not a plugin is installed, whether or not the
plugin is trusted, and whether or not Codex is running right now. Parsing it needs only read
access to a file on disk.

The parsing itself is `csr` (tools/session-reader): incremental byte offsets, parser context and
normalised `csr.event/v1` events in one SQLite database, committed per batch. This module adds a
single side table (`collector_enqueued`) so each event is handed to the spool exactly once.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

SESSIONS_DIR = "sessions"
_ENQUEUED_DDL = """
CREATE TABLE IF NOT EXISTS collector_enqueued (
    event_key TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS collector_enqueued_by_at ON collector_enqueued(at);
"""
_CANDIDATES = ("CSR_PATH",)  # env override first


class CsrMissing(RuntimeError):
    pass


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _candidates() -> list[Path]:
    root = _plugin_root()
    out = []
    env = os.environ.get("CSR_PATH")
    if env:
        out.append(Path(env))
    out.append(root / "lib")                      # packaged: lib\csr
    out.append(root.parents[1] / "tools" / "session-reader")  # dev checkout
    out.append(root.parents[1])
    return out


def load_csr():
    """Import csr, adding a known location to sys.path if it is not importable yet."""
    try:
        from csr import scanner, storage  # noqa: F401
        return scanner, storage
    except ImportError:
        pass
    for cand in _candidates():
        if (cand / "csr" / "__init__.py").is_file():
            sys.path.insert(0, str(cand))
            try:
                from csr import scanner, storage
                return scanner, storage
            except ImportError:
                sys.path.pop(0)
    raise CsrMissing("csr not found (set CSR_PATH to the tools/session-reader directory)")


def codex_home(environ=None) -> Path:
    env = os.environ if environ is None else environ
    value = env.get("CODEX_HOME")
    return Path(value) if value else Path.home() / ".codex"


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Reader:
    """Owns the csr SQLite database and the "already handed to the spool" marker."""

    def __init__(self, db_path: Path, *, home: Path | None = None):
        self.db_path = Path(db_path)
        self.home = Path(home) if home else codex_home()
        self._storage = None
        self._scanner = None

    # --- lifecycle --------------------------------------------------------------------------
    def open(self):
        if self._storage is None:
            self._scanner, storage_mod = load_csr()
            self._storage = storage_mod.Storage(self.db_path)
            self._storage.conn.executescript(_ENQUEUED_DDL)
            self._storage.conn.commit()
        return self._storage

    def close(self) -> None:
        if self._storage is not None:
            try:
                self._storage.close()
            finally:
                self._storage = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # --- scan -------------------------------------------------------------------------------
    @property
    def sessions_root(self) -> Path:
        return self.home / SESSIONS_DIR

    def scan(self, *, recursive: bool = True, max_bytes: int | None = None) -> dict:
        """One pass over the rollout files. Read-only with respect to Codex's own files."""
        storage = self.open()
        root = self.sessions_root
        result = {"root": str(root), "present": root.is_dir(), "files": 0, "new_events": 0, "errors": 0}
        if not root.is_dir():
            return result
        kwargs = {"recursive": recursive}
        if max_bytes is not None:
            kwargs["max_bytes"] = max_bytes
        try:
            for item in self._scanner.scan(storage, root, **kwargs):
                result["files"] += 1
                result["new_events"] += item.new_events
                if item.status == "error":
                    result["errors"] += 1
        except sqlite3.Error:
            result["errors"] += 1
        return result

    # --- hand-off ---------------------------------------------------------------------------
    def take(self, limit: int = 200) -> list[dict]:
        """Events that were never handed to the spool, oldest first. Marks them as handed off."""
        storage = self.open()
        rows = storage.conn.execute(
            "SELECT e.event_key, e.body FROM events e "
            "LEFT JOIN collector_enqueued x ON x.event_key = e.event_key "
            "WHERE x.event_key IS NULL "
            "ORDER BY e.session_id, e.file, e.offset, e.idx LIMIT ?", (int(limit),)).fetchall()
        out, keys = [], []
        for row in rows:
            key = row["event_key"] if not isinstance(row, tuple) else row[0]
            body = row["body"] if not isinstance(row, tuple) else row[1]
            try:
                doc = json.loads(body)
            except (TypeError, ValueError):
                keys.append(key)  # unparseable: never retry it
                continue
            if isinstance(doc, dict):
                out.append(doc)
            keys.append(key)
        if keys:
            stamp = _now()
            storage.conn.executemany(
                "INSERT OR IGNORE INTO collector_enqueued(event_key, at) VALUES (?,?)",
                [(k, stamp) for k in keys])
            storage.conn.commit()
        return out

    def unread(self) -> int:
        storage = self.open()
        row = storage.conn.execute(
            "SELECT COUNT(*) FROM events e LEFT JOIN collector_enqueued x "
            "ON x.event_key = e.event_key WHERE x.event_key IS NULL").fetchone()
        return int(row[0]) if row else 0

    def prune_enqueued(self, keep_days: int = 30) -> int:
        """The marker only has to outlive the events it covers; keep it bounded."""
        import time
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - keep_days * 86400))
        storage = self.open()
        cur = storage.conn.execute("DELETE FROM collector_enqueued WHERE at < ?", (cutoff,))
        storage.conn.commit()
        return cur.rowcount or 0

    def counts(self) -> dict:
        """Cheap numbers for the status panel (no text)."""
        storage = self.open()
        try:
            events = storage.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            sessions = storage.conn.execute("SELECT COUNT(DISTINCT session_id) FROM events").fetchone()[0]
        except sqlite3.Error:
            return {"events": 0, "sessions": 0}
        return {"events": int(events), "sessions": int(sessions)}
