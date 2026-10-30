"""Turn a `csr.event/v1` record into upload records, by tier.

Tiers exist so the cheap, uncontroversial data is never held hostage by the expensive kind:

    L0  metadata only      — kind, time, model, sizes, redaction counts, no text at all
    L1  structure          — tool names and argument *shapes*, code as language/lines/hash
    L2  content            — the prompt / reply / tool bodies themselves (already redacted by csr)
    L3  complete event     — every field csr produced, including per-record provenance

Every event yields exactly one L0 record; content records are additional. That way a device
that is offline for a week still delivers a complete metadata picture, and the backend can
tell "this turn had no content uploaded" from "this turn never happened".

`L3` supersedes `L2` for the same event (it contains it), so enabling both does not duplicate
anything: the more detailed tier wins.
"""
from __future__ import annotations

import hashlib
from collections import Counter

SCHEMA = "comp.event/v1"
SCHEMA_SUMMARY = "comp.session/v1"

# Kinds that carry conversation content (L2) vs. only structure (L1).
CONTENT_KINDS = {"user_prompt", "assistant_message", "reasoning_summary", "turn_error"}
TOOL_KINDS = {"tool_call", "tool_result", "tool_execution"}
META_KINDS = {"session_meta", "token_usage", "turn_coverage", "turn_meta"}

SHORT_STR = 64  # strings at most this long, on one line, are kept verbatim even at L1


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _sample(session_id: str, ratio: float) -> bool:
    """Deterministic per session: the same session is always in or always out."""
    if ratio >= 1.0:
        return True
    if ratio <= 0.0:
        return False
    bucket = int(hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < ratio


def structure(value, depth: int = 0):
    """Replace leaf text with a length/line/hash summary; keep keys, types and small scalars."""
    if depth > 6:
        return {"__depth__": True}
    if isinstance(value, dict):
        return {k: structure(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [structure(v, depth + 1) for v in value[:20]] + (
            [{"__more__": len(value) - 20}] if len(value) > 20 else [])
    if isinstance(value, str):
        if "\n" not in value and len(value) <= SHORT_STR:
            return value
        return {"__str__": {"len": len(value), "lines": value.count("\n") + 1, "sha1": _sha(value)}}
    return value


def _byte_len(value) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogatepass"))
    if isinstance(value, (dict, list, tuple)):
        return sum(_byte_len(v) for v in (value.values() if isinstance(value, dict) else value))
    return len(str(value))


def _text_chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_text_chars(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_text_chars(v) for v in value)
    return 0


def _seq(event: dict) -> int:
    ordinal = event.get("ordinal")
    if isinstance(ordinal, int) and ordinal >= 0:
        # csr packs (ordinal, index-within-record) as ordinal*1000+idx; the event_key's tail is idx
        tail = str(event.get("event_key") or "").rsplit(":", 1)[-1]
        idx = int(tail) if tail.isdigit() else 0
        return ordinal * 1000 + idx
    return -1


def _base(event: dict, tier: str) -> dict:
    return {
        "schema": SCHEMA,
        "event_id": f"{event.get('event_key')}#{tier}",
        "tier": tier,
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "turn_id_inferred": bool(event.get("turn_id_inferred")),
        "ordinal": event.get("ordinal"),
        "seq": _seq(event),
        "ts": event.get("ts"),
        "kind": event.get("kind"),
        "model": event.get("model"),
        "item_id": event.get("item_id"),
        "call_id": event.get("call_id"),
    }


def l0_record(event: dict) -> dict:
    """Metadata only: never a byte of user text."""
    data = event.get("data")
    rec = _base(event, "L0")
    rec["meta"] = {
        "data_keys": sorted(data) if isinstance(data, dict) else [],
        "data_bytes": _byte_len(data),
        "text_chars": _text_chars(data),
        "redactions": event.get("redactions") or {},
        "truncated": event.get("truncated") or [],
        "missing": [m.get("field") for m in (event.get("missing") or []) if isinstance(m, dict)],
        "codex_version": ((event.get("source") or {}).get("codex_version")),
    }
    return rec


def l1_record(event: dict) -> dict:
    """Structure only: names, keys, shapes, code fingerprints."""
    rec = _base(event, "L1")
    rec["data"] = structure(event.get("data"))
    return rec


def l2_record(event: dict, *, code_mode: str = "full") -> dict:
    """Content, as csr already redacted it. `code_mode=structure` replaces fenced code."""
    data = event.get("data")
    if code_mode == "structure":
        data = structure(data)
    elif code_mode == "hash":
        data = {"__sha1__": _sha(str(data)), "bytes": _byte_len(data)}
    rec = _base(event, "L2")
    rec["data"] = data
    return rec


def l3_record(event: dict) -> dict:
    """The complete event, including provenance — nothing held back on this machine."""
    rec = _base(event, "L3")
    rec["data"] = event.get("data")
    rec["raw"] = {
        "schema": event.get("schema"),
        "missing": event.get("missing") or [],
        "truncated": event.get("truncated") or [],
        "redactions": event.get("redactions") or {},
        "source": event.get("source") or {},
        "turn_id_inferred": bool(event.get("turn_id_inferred")),
    }
    return rec


def classify(event: dict) -> str:
    """The most detailed tier this event could contribute."""
    kind = event.get("kind")
    if kind in TOOL_KINDS:
        return "L1"
    if kind in CONTENT_KINDS:
        return "L2"
    if kind in META_KINDS:
        return "L0"
    return "L1" if event.get("data") else "L0"


def records_for(event: dict, *, tiers=("L0", "L1", "L2", "L3"), sample_ratio: float = 1.0,
                code_mode: str = "full") -> list[dict]:
    """All upload records for one event. L0 always; the rest depend on the event and the config."""
    out = [l0_record(event)] if "L0" in tiers else []
    kind = event.get("kind")
    session_id = str(event.get("session_id") or "")
    sampled = _sample(session_id, sample_ratio)
    full = "L3" in tiers
    if kind in CONTENT_KINDS:
        if sampled and full:
            out.append(l3_record(event))
        elif sampled and "L2" in tiers:
            out.append(l2_record(event, code_mode=code_mode))
    elif kind in TOOL_KINDS:
        if "L1" in tiers:
            out.append(l1_record(event))
        if sampled and full:
            out.append(l3_record(event))  # the tool output itself, which L1 deliberately drops
    else:
        if "L1" in tiers and event.get("data"):
            out.append(l1_record(event))
        if sampled and full:
            out.append(l3_record(event))
    return out


def session_summary(session_id: str, events: list[dict]) -> dict:
    """One telemetry record per session per scan: what Codex did, without repeating any text.

    The event id depends on the highest ordinal seen, so a later scan of the same session
    produces a *new* summary and the backend simply stores both — no dedupe games needed.
    """
    kinds = Counter(e.get("kind") for e in events if e.get("kind"))
    models = sorted({e.get("model") for e in events if isinstance(e.get("model"), str)})
    ordinals = [e["ordinal"] for e in events if isinstance(e.get("ordinal"), int)]
    stamps = sorted(e["ts"] for e in events if isinstance(e.get("ts"), str))
    turns = {e.get("turn_id") for e in events if e.get("turn_id")}
    versions = sorted({((e.get("source") or {}).get("codex_version")) for e in events
                       if (e.get("source") or {}).get("codex_version")})
    top = max(ordinals) if ordinals else -1
    return {
        "schema": SCHEMA_SUMMARY,
        "event_id": f"summary:{session_id}:{top}",
        "tier": "L0",
        "kind": "session_summary",
        "session_id": session_id,
        "turn_id": None,
        "ordinal": top if top >= 0 else None,
        "seq": top * 1000,
        "ts": stamps[-1] if stamps else None,
        "model": models[-1] if models else None,
        "item_id": None,
        "call_id": None,
        "meta": {
            "events": len(events),
            "kinds": dict(kinds),
            "models": models,
            "turns": len(turns),
            "first_ts": stamps[0] if stamps else None,
            "last_ts": stamps[-1] if stamps else None,
            "codex_version": versions[-1] if versions else None,
            "tool_calls": sum(kinds.get(k, 0) for k in TOOL_KINDS),
            "errors": kinds.get("turn_error", 0),
        },
    }
