"""Per-turn coverage: at the end of a turn, say what was captured and what is missing, and why.

Events are tallied per turn in the session cursor as they are recorded; when the turn ends a
derived `turn_coverage` event lists each item as captured / partial / unavailable (the source
said it has none) / not_captured (expected but never seen) / none (the turn had none).
Best effort: an async hook processed after the turn ended is not counted.
"""
from __future__ import annotations

from .events import COMPLETE, PARTIAL, TRUNCATED, UNAVAILABLE, make_event

MAX_OPEN_TURNS = 50
MAX_COVERED = 200
_NOT_TALLIED = {"turn_coverage", "hook_invocation", "recorder_notice", "transcript_other", "session_meta"}
_END_KINDS = {"turn_end", "turn_interrupted"}
NO_TRANSCRIPT = "会话无 transcript（Codex 不持久化），无法获取"
MISSING_STATES = {"not_captured", "unavailable"}


def update(cursor: dict, events: list[dict], background: bool, session_id: str, meta: dict) -> list[dict]:
    """Tally `events` and return turn_coverage events for turns that ended among them."""
    tallies = cursor.setdefault("turn_tally", {})
    covered = cursor.setdefault("covered_turns", [])
    for e in events:
        _tally(tallies, covered, e)
    out = []
    for e in events:
        tid = e.get("turn_id")
        if not tid or tid in covered or e["kind"] not in _END_KINDS:
            continue
        # Standard sessions end on the transcript record (it also sees failed turns);
        # background sessions have no transcript, so the Stop / Interrupt hook ends them.
        if e["source"]["type"] != ("hook" if background else "transcript"):
            continue
        tally = tallies.pop(tid, {"k": {}, "models": []})
        out.append(_coverage(tid, tally, e, background, session_id, meta))
        covered.append(tid)
        del covered[:-MAX_COVERED]
    return out


def _tally(tallies: dict, covered: list, e: dict) -> None:
    tid = e.get("turn_id")
    if not tid or tid in covered or e["kind"] in _NOT_TALLIED:
        return
    t = tallies.setdefault(tid, {"k": {}, "models": []})
    key = f'{e["kind"]}:{e["completeness"]["status"]}'
    t["k"][key] = t["k"].get(key, 0) + 1
    if e["kind"] == "tool_result" and (e.get("data") or {}).get("outcome") == "unknown":
        t["k"]["tool_result:outcome_unknown"] = t["k"].get("tool_result:outcome_unknown", 0) + 1
    if e.get("model") and e["model"] not in t["models"]:
        t["models"].append(e["model"])
    while len(tallies) > MAX_OPEN_TURNS:
        tallies.pop(next(iter(tallies)))


def _count(k: dict, kind: str, *statuses: str) -> int:
    statuses = statuses or (COMPLETE, PARTIAL, TRUNCATED, UNAVAILABLE)
    return sum(k.get(f"{kind}:{s}", 0) for s in statuses)


def _item(status: str, note: str = "", **extra) -> dict:
    item = {"status": status, **extra}
    if note:
        item["note"] = note
    return item


def _turn_status(end: dict, k: dict) -> str:
    if end["kind"] == "turn_interrupted":
        return "interrupted"
    return "failed" if _count(k, "turn_error") else "completed"


def _items(k: dict, models: list, completed: bool, background: bool) -> dict:
    items = {}
    prompts = _count(k, "user_prompt")
    items["user_prompt"] = _item("captured" if prompts else "not_captured", count=prompts)

    full = _count(k, "assistant_message", COMPLETE, TRUNCATED)
    last_only = _count(k, "assistant_message", PARTIAL)
    if full:
        items["assistant_messages"] = _item("captured", count=full)
    elif last_only:
        items["assistant_messages"] = _item("partial", "只有 Stop 提供的最后一条消息", count=last_only)
    elif background:
        items["assistant_messages"] = _item("unavailable", NO_TRANSCRIPT, count=0)
    else:
        items["assistant_messages"] = _item("not_captured" if completed else "none", count=0)

    if background:
        items["reasoning_summary"] = _item("unavailable", NO_TRANSCRIPT)
    elif _count(k, "reasoning_summary", COMPLETE, TRUNCATED):
        items["reasoning_summary"] = _item("captured")
    elif _count(k, "reasoning_summary", UNAVAILABLE):
        items["reasoning_summary"] = _item("unavailable", "模型未返回公开推理摘要")
    else:  # coverage fires on the transcript's turn end, so the whole turn was read
        items["reasoning_summary"] = _item("unavailable", "本回合 transcript 中没有推理条目（模型未返回推理或摘要）")

    items["model"] = _item("captured" if models else "not_captured", values=list(models))

    calls, results = _count(k, "tool_call"), _count(k, "tool_result")
    items["tool_calls"] = _item("captured" if calls or results else "none", call_events=calls,
                                result_events=results, outcome_unknown=k.get("tool_result:outcome_unknown", 0))

    if background:
        items["token_usage"] = _item("unavailable", NO_TRANSCRIPT)
    elif _count(k, "token_usage"):
        items["token_usage"] = _item("captured")
    else:
        items["token_usage"] = _item("not_captured" if completed else "none")

    items["timestamps"] = _item("partial", "Hook 事件没有来源时间戳，只有记录时间 recorded_at")
    return items


def _coverage(tid: str, tally: dict, end: dict, background: bool, session_id: str, meta: dict) -> dict:
    k, models = tally["k"], tally["models"]
    turn_status = _turn_status(end, k)
    items = _items(k, models, turn_status == "completed", background)
    missing = [name for name, item in items.items() if item["status"] in MISSING_STATES]
    return make_event("turn_coverage", session_id=session_id, source_type="derived",
                      source_name="recorder.coverage", turn_id=tid, source_ts=end.get("source_ts"),
                      agent_id=end.get("agent_id"), meta=meta, model=models[-1] if models else None,
                      data={"turn_status": turn_status, "items": items, "missing": missing},
                      missing=missing, note="回合结束时的捕获情况（尽力而为）")
