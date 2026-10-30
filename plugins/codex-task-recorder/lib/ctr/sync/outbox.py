"""The upload queue is the recorder's session files; each file has an upload cursor.

A batch is built from the complete lines after each file's cursor: events outside the
sync consent windows are skipped (and consumed), the rest are projected for upload. The
cursor moves only in `commit`, after the backend acknowledged the batch, so a crash or a
lost response just rebuilds the same batch (same event_ids; the backend deduplicates).

Session files are found by listing sessions\\*\\*.jsonl: the uploader never opens the
recorder's cursor files, which hooks replace on every invocation (on Windows a reader holding
a file open makes that replace fail). Session files are only ever appended to.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .. import store
from ..events import SCHEMA
from ..paths import Paths
from ..redact import redact_obj
from .config import SyncPaths
from .state import SyncState, in_window, is_on

MAX_EVENTS = 200
MAX_BYTES = 1024 * 1024
HEAD_BYTES = 128  # identifies a session file: its first event line starts with a unique event_id
BATCH_SCHEMA = "ctr.sync.batch/v1"
OLD_SCHEMA, OUTSIDE_WINDOW, BAD_LINE, REWOUND = "old_schema", "outside_sync_window", "bad_line", "file_rewound"
BEFORE_SYNC = "before_sync"
HOOK_INVOCATION_KEEP = ("payload_keys", "transcript_path_present", "notice_emitted", "duration_ms")
ACKED = {"accepted", "duplicate"}

# Sent with every batch so the backend can tell "not collected" from "lost" (docs/sync-api.md §4).
CAPABILITIES = {
    "not_collected_by_policy": ["encrypted_reasoning", "raw_reasoning", "system_instructions_text",
                                "rate_limits", "account_info", "credential_file_contents",
                                "recorder_diagnostics"],
    "not_provided_by_codex": ["hook_source_timestamps", "post_tool_use_exit_code",
                              "stop_hook_on_failed_or_interrupted_turn", "background_session_transcript"],
    "not_verified": ["PermissionRequest", "PostCompact", "SubagentStart", "SubagentStop"],
}


def skip_reason(st: SyncState, event: dict) -> str | None:
    """Why an event must not be uploaded, or None when it may be."""
    if not isinstance(event.get("event_id"), str) or not event["event_id"]:
        return BAD_LINE  # no idempotency key: it could never be acknowledged
    if not in_window(st, event.get("recorded_at")):
        return OUTSIDE_WINDOW
    # Content written before sync was on (or while paused) stays local even if read later.
    if event.get("kind") != "session_meta" and event.get("source_ts") and not in_window(st, event["source_ts"]):
        return OUTSIDE_WINDOW
    if event.get("schema") != SCHEMA or not isinstance(event.get("seq"), int):
        return OLD_SCHEMA
    return None


def project(event: dict) -> dict:
    """What leaves the machine: no recorder diagnostics, and one more redaction pass."""
    out = copy.deepcopy(event)
    data = out.get("data") if isinstance(out.get("data"), dict) else {}
    if out.get("kind") == "hook_invocation":
        data = {k: data[k] for k in HOOK_INVOCATION_KEEP if k in data}
    redacted, counts = redact_obj(data)
    out["data"] = redacted
    if counts and redacted != data:  # only rules that found something the recorder missed
        red = out.setdefault("redaction", {})
        red["applied"] = True
        red["upload_pass"] = dict(counts)
    return out


def _identity(file: Path, n: int) -> str:
    with open(file, "rb") as f:
        return hashlib.sha256(f.read(n)).hexdigest()[:32]


@dataclass
class Progress:
    key: str
    file: Path
    rel: str
    cursor: dict
    offset: int
    rewound: bool = False
    session_id: str | None = None
    last_seq: int | None = None
    event_ids: list = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


@dataclass
class Batch:
    events: list
    progress: dict  # file key -> Progress

    def envelope(self, batch_id: str, client: str) -> dict:
        return {"schema": BATCH_SCHEMA, "batch_id": batch_id, "client": client,
                "capabilities": CAPABILITIES, "events": self.events}


@dataclass
class _Source:
    key: str
    file: Path
    rel: str
    cursor: dict


class Outbox:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.sp = SyncPaths(paths)

    # ---- cursors ---------------------------------------------------------------------------
    def _cursor_path(self, key: str) -> Path:
        return self.sp.cursors / f"{key}.json"

    def _load_cursor(self, key: str) -> dict:
        try:
            doc = json.loads(self._cursor_path(key).read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cursor(self, key: str, cursor: dict) -> None:
        self.sp.cursors.mkdir(parents=True, exist_ok=True)
        path = self._cursor_path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cursor, ensure_ascii=False), encoding="utf-8")
        store.replace(tmp, path)

    def prune(self) -> int:
        """Remove upload cursors whose session file was deleted (`ctr purge`)."""
        if not self.sp.cursors.exists():
            return 0
        live = {s.key for s in self._sources()}
        removed = 0
        for path in self.sp.cursors.glob("*.json"):
            if path.stem not in live:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def start_from_now(self) -> int:
        """Under a new consent, skip everything already recorded without reading it again later.
        Returns the number of lines skipped."""
        skipped = 0
        for src in self._sources():
            with open(src.file, "rb") as f:
                data = f.read()
            offset = data.rfind(b"\n") + 1  # never inside a line a hook is still writing
            lines = data.count(b"\n")
            cursor = {"file": src.rel, "offset": offset, "uploaded": src.cursor.get("uploaded", 0),
                      "rejected": src.cursor.get("rejected", 0),
                      "skipped": dict(Counter(src.cursor.get("skipped") or {}) + Counter({BEFORE_SYNC: lines}))}
            if offset:
                cursor["head_len"] = min(HEAD_BYTES, offset)
                cursor["head"] = _identity(src.file, cursor["head_len"])
            self._save_cursor(src.key, cursor)
            skipped += lines
        return skipped

    # ---- sources ---------------------------------------------------------------------------
    def _sources(self) -> list[_Source]:
        out = []
        if not self.paths.sessions.exists():
            return out
        for file in self.paths.sessions.glob("*/*.jsonl"):
            key = f"{file.parent.name}.{file.stem}"
            out.append(_Source(key, file, f"sessions/{file.parent.name}/{file.name}", self._load_cursor(key)))
        out.sort(key=lambda s: s.file.stat().st_mtime)  # older sessions drain first
        return out

    @staticmethod
    def _position(src: _Source) -> tuple[int, bool]:
        """Where to continue reading, and whether the file turned out to be a different one."""
        offset = int(src.cursor.get("offset") or 0)
        if offset > src.file.stat().st_size:
            return 0, True
        head, head_len = src.cursor.get("head"), src.cursor.get("head_len")
        if offset and head and isinstance(head_len, int) and _identity(src.file, head_len) != head:
            return 0, True  # deleted and recorded again: same name, different content
        return offset, False

    @staticmethod
    def _read(file: Path, offset: int, want: int) -> bytes:
        """Complete lines from `offset` (at least one if any exists)."""
        limit = want
        with open(file, "rb") as f:
            while True:
                f.seek(offset)
                chunk = f.read(limit)
                end = chunk.rfind(b"\n")
                if end >= 0 or len(chunk) < limit:
                    return chunk[: end + 1] if end >= 0 else b""
                limit *= 2  # one line longer than the chunk

    # ---- batches ---------------------------------------------------------------------------
    def next_batch(self, st: SyncState, max_events: int = MAX_EVENTS,
                   max_bytes: int = MAX_BYTES) -> Batch | None:
        if not is_on(st):  # paused or revoked: upload nothing, and leave the cursors alone
            return None
        batch = Batch([], {})
        size = 0
        full = False
        for src in self._sources():
            offset, rewound = self._position(src)
            if src.file.stat().st_size == offset and not rewound:
                continue
            chunk = self._read(src.file, offset, max(2 * max_bytes, 64 * 1024))
            prog = Progress(src.key, src.file, src.rel, src.cursor, offset, rewound)
            if rewound:
                prog.skipped[REWOUND] += 1
            pos = offset
            for raw in chunk.split(b"\n")[:-1]:
                line_end = pos + len(raw) + 1
                try:
                    event = json.loads(raw)
                    reason = skip_reason(st, event) if isinstance(event, dict) else BAD_LINE
                except ValueError:
                    event, reason = None, BAD_LINE
                if reason:
                    prog.skipped[reason] += 1
                    pos = prog.offset = line_end
                    continue
                projected = project(event)
                n = len(json.dumps(projected, ensure_ascii=False).encode("utf-8"))
                if batch.events and (len(batch.events) >= max_events or size + n > max_bytes):
                    full = True
                    break
                batch.events.append(projected)
                prog.event_ids.append(projected["event_id"])
                prog.session_id = projected.get("session_id") or prog.session_id
                prog.last_seq = projected["seq"]
                size += n
                pos = prog.offset = line_end
            if prog.offset != offset or prog.skipped:
                batch.progress[src.key] = prog
            if full:
                break
        return batch if batch.events or batch.progress else None

    def commit(self, batch: Batch, statuses: dict) -> Counter:
        """Move cursors past an acknowledged batch. `statuses` maps event_id to the backend's
        accepted / duplicate / rejected."""
        totals: Counter = Counter()
        for key, prog in batch.progress.items():
            cur = prog.cursor
            cur.update(file=prog.rel, offset=prog.offset)
            if prog.session_id:
                cur["session_id"] = prog.session_id
            if prog.offset and (prog.rewound or "head" not in cur):
                cur["head_len"] = min(HEAD_BYTES, prog.offset)
                cur["head"] = _identity(prog.file, cur["head_len"])
            if prog.last_seq is not None:
                cur["last_seq"] = prog.last_seq
            uploaded = sum(1 for eid in prog.event_ids if statuses.get(eid) in ACKED)
            rejected = sum(1 for eid in prog.event_ids if statuses.get(eid) == "rejected")
            cur["uploaded"] = cur.get("uploaded", 0) + uploaded
            cur["rejected"] = cur.get("rejected", 0) + rejected
            skipped = Counter(cur.get("skipped") or {})
            skipped.update(prog.skipped)
            cur["skipped"] = dict(skipped)
            self._save_cursor(key, cur)
            totals["uploaded"] += uploaded
            totals["rejected"] += rejected
            totals["skipped"] += sum(prog.skipped.values())
        return totals

    # ---- status ----------------------------------------------------------------------------
    def backlog(self, st: SyncState | None) -> tuple[int, int]:
        """(events, sessions) still to upload under the current consent."""
        if st is None:
            return 0, 0
        events = sessions = 0
        for src in self._sources():
            offset, _ = self._position(src)
            if src.file.stat().st_size == offset:
                continue
            n = 0
            for raw in self._read(src.file, offset, 1 << 30).split(b"\n")[:-1]:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(event, dict) and skip_reason(st, event) is None:
                    n += 1
            if n:
                events += n
                sessions += 1
        return events, sessions

    def totals(self) -> Counter:
        """Uploaded / rejected / skipped counts over all session files."""
        out: Counter = Counter()
        if not self.sp.cursors.exists():
            return out
        for path in self.sp.cursors.glob("*.json"):
            cur = self._load_cursor(path.stem)
            out["uploaded"] += cur.get("uploaded", 0)
            out["rejected"] += cur.get("rejected", 0)
            for reason, n in (cur.get("skipped") or {}).items():
                out[f"skipped:{reason}"] += n
        return out
