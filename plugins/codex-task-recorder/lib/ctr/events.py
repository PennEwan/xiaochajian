"""Build ctr.event/v2 records: redaction, truncation and completeness in one place.

v2 = v1 + `seq` (per-session capture order, set by store.SessionHandle.append) and
`model` (the model in effect for the event, set by the hook handler and transcript reader).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter

from . import timeutil
from .redact import redact_obj

SCHEMA = "ctr.event/v2"
MAX_FIELD_CHARS = 64 * 1024

COMPLETE, PARTIAL, TRUNCATED, UNAVAILABLE = "complete", "partial", "truncated", "unavailable"


def _truncate(value: object, path: str, notes: list[dict]) -> object:
    if isinstance(value, str):
        if len(value) <= MAX_FIELD_CHARS:
            return value
        notes.append({"path": path, "original_length": len(value),
                      "sha256": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()})
        return value[:MAX_FIELD_CHARS]
    if isinstance(value, dict):
        return {k: _truncate(v, f"{path}.{k}", notes) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, f"{path}[{i}]", notes) for i, v in enumerate(value)]
    return value


def make_event(kind: str, *, session_id: str, source_type: str, source_name: str,
               data: dict, turn_id: str | None = None, tool_use_id: str | None = None,
               item_id: str | None = None, agent_id: str | None = None,
               relations: dict | None = None, source_ts: str | None = None,
               granularity: str | None = None, missing: list[str] | None = None,
               status: str | None = None, note: str = "", meta: dict | None = None,
               transcript_offset: int | None = None,
               extra_redactions: Counter | None = None, model: str | None = None) -> dict:
    redacted, counts = redact_obj(data)
    if extra_redactions:
        counts.update(extra_redactions)
    truncated: list[dict] = []
    redacted = _truncate(redacted, "data", truncated)
    missing = list(missing or [])
    if status is None:
        status = TRUNCATED if truncated else (PARTIAL if missing else COMPLETE)
    completeness = {"status": status, "missing": missing, "note": note}
    if truncated:
        completeness["truncated_fields"] = truncated
    meta = meta or {}
    return {
        "schema": SCHEMA,
        "event_id": str(uuid.uuid4()),
        "seq": None,  # set when appended to the session file
        "kind": kind,
        "recorded_at": timeutil.iso(timeutil.now()),
        "source_ts": source_ts,
        "session_id": session_id,
        "session_kind": None,  # standard | background, set by the hook handler
        "turn_id": turn_id,
        "tool_use_id": tool_use_id,
        "item_id": item_id,
        "agent_id": agent_id,
        "model": model,
        "relations": relations or {},
        "source": {
            "type": source_type,
            "name": source_name,
            "codex_version": meta.get("codex_version"),
            "originator": meta.get("originator"),
            "transcript_offset": transcript_offset,
        },
        "granularity": granularity,
        "completeness": completeness,
        "redaction": {"applied": bool(counts), "counts": dict(counts)},
        "data": redacted,
    }


_EXIT_CODE = re.compile(r"(?i)\bexit(?:ed with)?\s*code\s*[:=]?\s*(-?\d+)")
_ERROR_STATUSES = {"failed", "error", "declined", "rejected", "cancelled", "canceled", "timed_out"}
_OK_STATUSES = {"completed", "success", "succeeded", "ok"}


def detect_outcome(value: object) -> str:
    """success / error / unknown, from whatever shape a tool result has."""
    if isinstance(value, dict):
        for key in ("exit_code", "exitCode"):
            code = value.get(key)
            if isinstance(code, int) and not isinstance(code, bool):
                return "success" if code == 0 else "error"
        if value.get("is_error") is True or value.get("isError") is True:
            return "error"
        if value.get("success") is False or value.get("error"):
            return "error"
        status = value.get("status")
        if isinstance(status, str):
            if status.lower() in _ERROR_STATUSES:
                return "error"
            if status.lower() in _OK_STATUSES:
                return "success"
        if value.get("success") is True:
            return "success"
        for key in ("output", "stdout", "content"):
            if isinstance(value.get(key), str):
                found = detect_outcome(value[key])
                if found != "unknown":
                    return found
        return "unknown"
    if isinstance(value, str):
        m = _EXIT_CODE.search(value)
        if m:
            return "success" if int(m.group(1)) == 0 else "error"
        if value.lstrip().startswith("{"):
            try:
                return detect_outcome(json.loads(value))
            except ValueError:
                pass
    return "unknown"


def fingerprint(value: object) -> dict:
    """Hash and length of content we deliberately do not store."""
    if value is None:
        return {"present": False}
    text = value if isinstance(value, str) else repr(value)
    return {"present": True, "length": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()}
