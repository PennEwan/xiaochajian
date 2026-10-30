"""Incremental reader for the transcript Codex passes to hooks as `transcript_path`.

Codex documents this file as "not a stable interface for hooks", so only record
types observed in Codex 0.162/0.163 rollouts are mapped. Everything else is
counted by type name and never stored. Records outside a consent window are
skipped, so nothing from before `enable` or after `disable` is backfilled.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field

from .events import UNAVAILABLE, detect_outcome, fingerprint, make_event
from .redact import SENSITIVE_FILE_PLACEHOLDER, mentions_sensitive_file
from .state import State, in_window

MAX_SENSITIVE_CALLS = 500


@dataclass
class Context:
    session_id: str
    meta: dict
    sensitive_calls: list = field(default_factory=list)
    agent_id: str | None = None
    last_model: str | None = None
    current_turn: str | None = None


# Records of these kinds sit inside a turn even when the transcript omits turn_id.
_TURN_SCOPED = {"tool_call", "tool_result", "token_usage", "assistant_message",
                "reasoning_summary", "user_prompt", "turn_error"}


@dataclass
class IngestResult:
    events: list
    offset: int
    stats: Counter


def ingest(path: str, offset: int, state: State, ctx: Context, budget_bytes: int) -> IngestResult:
    stats: Counter = Counter()
    try:
        f = open(path, "rb")
    except OSError:
        stats["unreadable_transcript"] += 1
        return IngestResult([], offset, stats)
    with f:
        size = os.fstat(f.fileno()).st_size
        if offset > size:  # file was replaced or truncated
            stats["transcript_rewound"] += 1
            offset = 0
        f.seek(offset)
        chunk = f.read(budget_bytes)
    end = chunk.rfind(b"\n")
    if end < 0:  # no complete line yet; never consume a partial line
        return IngestResult([], offset, stats)
    events: list[dict] = []
    pos = offset
    for raw in chunk[: end + 1].split(b"\n")[:-1]:
        line_offset, pos = pos, pos + len(raw) + 1
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            stats["bad_line"] += 1
            continue
        if not isinstance(rec, dict):
            stats["bad_line"] += 1
            continue
        mapped = _map_record(rec, line_offset, state, ctx, stats)
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if rec.get("type") == "turn_context" or payload.get("type") == "task_started":
            ctx.current_turn = payload.get("turn_id") or ctx.current_turn
        for e in mapped:
            # turn_context precedes a turn's records, so the latest model seen is that turn's model.
            if e["model"] is None:
                e["model"] = ctx.last_model
            if e["turn_id"] is None and ctx.current_turn and e["kind"] in _TURN_SCOPED:
                e["turn_id"] = ctx.current_turn
                e["relations"]["turn_id_inferred"] = True
        events.extend(mapped)
    return IngestResult(events, offset + end + 1, stats)


def _text_parts(content: object) -> tuple[str, list[str]]:
    texts, other = [], []
    if isinstance(content, str):
        return content, []
    for part in content or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif isinstance(part, dict):
            other.append(str(part.get("type")))
    return "\n".join(texts), other


def _duration_ms(value: object) -> int | None:
    if isinstance(value, dict) and "secs" in value:
        return int(value.get("secs", 0)) * 1000 + int(value.get("nanos", 0)) // 1_000_000
    return None


def _sub(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, dict) else value


def _map_record(rec: dict, offset: int, state: State, ctx: Context, stats: Counter) -> list[dict]:
    rtype = rec.get("type")
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    ts = rec.get("timestamp")

    def ev(kind: str, name: str, data: dict, **kw) -> dict:
        return make_event(kind, session_id=ctx.session_id, source_type="transcript",
                          source_name=name, data=data, source_ts=ts, meta=ctx.meta,
                          transcript_offset=offset, agent_id=ctx.agent_id, **kw)

    if rtype == "session_meta":
        # Config only, allowed regardless of consent window: no conversation content.
        ctx.meta["codex_version"] = payload.get("cli_version")
        ctx.meta["originator"] = payload.get("originator")
        return [ev("session_meta", "session_meta", {
            "thread_id": payload.get("id"), "cli_version": payload.get("cli_version"),
            "originator": payload.get("originator"), "source": payload.get("source"),
            "model_provider": payload.get("model_provider"),
            "context_window": payload.get("context_window"),
            "history_mode": payload.get("history_mode"), "cwd": payload.get("cwd"),
            "base_instructions": fingerprint(payload.get("base_instructions")),
        })]
    if ts is None:
        stats["no_timestamp"] += 1
        return []
    if not in_window(state, ts):
        stats["skipped_outside_consent_window"] += 1
        return []

    sub = payload.get("type")
    if rtype == "turn_context":
        return _model_config(ev, "turn_context", "turn", payload.get("turn_id"), {
            "model": payload.get("model"), "reasoning_effort": payload.get("effort"),
            "reasoning_summary": payload.get("summary"),
            "approval_policy": payload.get("approval_policy"),
            "approvals_reviewer": payload.get("approvals_reviewer"),
            "sandbox_policy": _sub(payload.get("sandbox_policy"), "type"),
            "network_access": _sub(payload.get("sandbox_policy"), "network_access"),
            "collaboration_mode": _sub(payload.get("collaboration_mode"), "mode"),
            "permission_profile": _sub(payload.get("permission_profile"), "type"),
            "cwd": payload.get("cwd"),
        }, ctx)
    if rtype == "event_msg":
        return _event_msg(ev, sub, payload, ctx, stats)
    if rtype == "response_item":
        return _response_item(ev, sub, payload, ctx, stats)
    if rtype == "world_state":
        stats["omitted:world_state"] += 1
    elif rtype == "token_usage_record":
        stats["duplicate:token_usage_record"] += 1
    else:
        stats[f"other:{rtype}"] += 1
    return []


def _model_config(ev, name: str, scope: str, turn_id: object, data: dict, ctx: Context) -> list[dict]:
    data["config_scope"] = scope
    missing = [k for k in ("model", "reasoning_effort") if data.get(k) is None]
    out = [ev("model_config", name, data, turn_id=turn_id, missing=missing)]
    model = data.get("model")
    if model and ctx.last_model and model != ctx.last_model:
        out.append(make_event("model_changed", session_id=ctx.session_id, source_type="derived",
                              source_name=f"{name}.model", turn_id=turn_id, meta=ctx.meta,
                              agent_id=ctx.agent_id, source_ts=out[0]["source_ts"],
                              data={"from": ctx.last_model, "to": model}))
    if model:
        ctx.last_model = model
    return out


def _event_msg(ev, sub, p: dict, ctx: Context, stats: Counter) -> list[dict]:
    name = f"event_msg/{sub}"
    turn_id = p.get("turn_id")
    if sub == "thread_settings_applied":
        s = p.get("thread_settings") if isinstance(p.get("thread_settings"), dict) else {}
        return _model_config(ev, name, "thread", turn_id, {
            "model": s.get("model"), "model_provider_id": s.get("model_provider_id"),
            "reasoning_effort": s.get("reasoning_effort"),
            "reasoning_summary": s.get("reasoning_summary"), "service_tier": s.get("service_tier"),
            "approval_policy": s.get("approval_policy"),
            "approvals_reviewer": s.get("approvals_reviewer"), "personality": s.get("personality"),
            "collaboration_mode": _sub(s.get("collaboration_mode"), "mode"), "cwd": s.get("cwd"),
        }, ctx)
    if sub == "task_started":
        attr = p.get("turn_attribution") if isinstance(p.get("turn_attribution"), dict) else {}
        return [ev("turn_start", name, {
            "started_at": p.get("started_at"),
            "collaboration_mode_kind": p.get("collaboration_mode_kind"),
            "model_context_window": p.get("model_context_window"),
            "turn_trigger": attr.get("turn_trigger"),
        }, turn_id=turn_id, relations={
            "parent_turn_id": attr.get("parent_turn_id"), "root_turn_id": attr.get("root_turn_id"),
            "initiating_agent_path": attr.get("initiating_agent_path")})]
    if sub == "task_complete":
        out = [ev("turn_end", name, {
            "started_at": p.get("started_at"), "completed_at": p.get("completed_at"),
            "duration_ms": p.get("duration_ms"), "time_to_first_token_ms": p.get("time_to_first_token_ms"),
            "last_agent_message": p.get("last_agent_message")}, turn_id=turn_id)]
        err = p.get("error")
        if err:
            out.append(ev("turn_error", name, {
                "message": _sub(err, "message"), "codex_error_info": _sub(err, "codex_error_info")},
                turn_id=turn_id))
        return out
    if sub == "turn_aborted":
        return [ev("turn_interrupted", name, {
            "reason": p.get("reason"), "duration_ms": p.get("duration_ms"),
            "started_at": p.get("started_at"), "completed_at": p.get("completed_at")}, turn_id=turn_id)]
    if sub == "error":
        return [ev("turn_error", name, {"message": p.get("message"),
                                        "codex_error_info": p.get("codex_error_info")}, turn_id=turn_id)]
    if sub == "token_count":
        if p.get("rate_limits") is not None:
            stats["omitted:rate_limits"] += 1
        info = p.get("info")
        if not isinstance(info, dict):
            stats["other:token_count_without_info"] += 1
            return []
        return [ev("token_usage", name, {
            "last_token_usage": info.get("last_token_usage"),
            "total_token_usage": info.get("total_token_usage"),
            "model_context_window": info.get("model_context_window")})]
    if sub == "item_completed":
        return _item(ev, p, ctx, stats)
    stats[f"other:{name}"] += 1
    return []


def _item(ev, p: dict, ctx: Context, stats: Counter) -> list[dict]:
    it = p.get("item") if isinstance(p.get("item"), dict) else {}
    itype = it.get("type")
    name = f"event_msg/item_completed:{itype}"
    kw = {"turn_id": p.get("turn_id"), "item_id": it.get("id")}
    timing = {"started_at_ms": p.get("started_at_ms"), "completed_at_ms": p.get("completed_at_ms")}

    if itype == "UserMessage":
        text, other = _text_parts(it.get("content"))
        return [ev("user_prompt", name, {"text": text, "non_text_parts": other, **timing}, **kw)]
    if itype == "AgentMessage":
        text, other = _text_parts(it.get("content"))
        return [ev("assistant_message", name, {"text": text, "phase": it.get("phase"),
                                               "non_text_parts": other, **timing}, **kw)]
    if itype == "Reasoning":
        summary = it.get("summary_text")
        text = "\n".join(s for s in summary if isinstance(s, str)) if isinstance(summary, list) else summary
        raw_present = bool(it.get("raw_content"))
        if raw_present:
            stats["omitted:reasoning_raw_content"] += 1
        data = {"summary": text or None, "raw_content": {"omitted": True, "present": raw_present}, **timing}
        if text:
            return [ev("reasoning_summary", name, data, **kw)]
        return [ev("reasoning_summary", name, data, status=UNAVAILABLE, missing=["summary"],
                   note="模型未返回公开推理摘要；原始/加密推理按策略不采集", **kw)]

    tool_kw = {**kw, "granularity": "ui_item"}
    if itype == "CommandExecution":
        command = it.get("command")
        output, extra = it.get("aggregated_output"), None
        if mentions_sensitive_file(command):
            output, extra = SENSITIVE_FILE_PLACEHOLDER, Counter(sensitive_file=1)
        return [ev("tool_result", name, {
            "tool_name": "CommandExecution", "command": command, "cwd": it.get("cwd"),
            "exit_code": it.get("exit_code"), "status": it.get("status"),
            "duration_ms": _duration_ms(it.get("duration")), "source": it.get("source"),
            "parsed_cmd": it.get("parsed_cmd"), "output": output,
            "outcome": detect_outcome({"exit_code": it.get("exit_code"), "status": it.get("status")}),
            **timing}, extra_redactions=extra, **tool_kw)]
    if itype == "McpToolCall":
        args, result, extra = it.get("arguments"), it.get("result"), None
        if mentions_sensitive_file(args):
            result, extra = SENSITIVE_FILE_PLACEHOLDER, Counter(sensitive_file=1)
        outcome = detect_outcome({"error": it.get("error"), "status": it.get("status"),
                                  "isError": _sub(it.get("result"), "isError")})
        return [ev("tool_result", name, {
            "tool_name": f"mcp:{it.get('server')}/{it.get('tool')}", "server": it.get("server"),
            "tool": it.get("tool"), "plugin_id": it.get("pluginId"), "arguments": args,
            "result": result, "error": it.get("error"), "status": it.get("status"),
            "duration_ms": _duration_ms(it.get("duration")), "outcome": outcome, **timing},
            extra_redactions=extra, **tool_kw)]
    if itype == "FileChange":
        return [ev("tool_result", name, {
            "tool_name": "FileChange", "changes": it.get("changes"), "status": it.get("status"),
            "stdout": it.get("stdout"), "stderr": it.get("stderr"),
            "outcome": detect_outcome({"status": it.get("status")}), **timing}, **tool_kw)]
    if itype == "ImageView":
        return [ev("tool_result", name, {"tool_name": "ImageView", "path": it.get("path"),
                                         "outcome": "unknown", **timing}, **tool_kw)]
    stats[f"other:item/{itype}"] += 1
    return []


def _remember_sensitive(ctx: Context, call_id: object) -> None:
    if call_id and call_id not in ctx.sensitive_calls:
        ctx.sensitive_calls.append(call_id)
        del ctx.sensitive_calls[:-MAX_SENSITIVE_CALLS]


def _response_item(ev, sub, p: dict, ctx: Context, stats: Counter) -> list[dict]:
    name = f"response_item/{sub}"
    call_id = p.get("call_id")
    kw = {"tool_use_id": call_id, "granularity": "model_tool_call"}
    if sub == "function_call":
        args = p.get("arguments")
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            pass
        if mentions_sensitive_file(args):
            _remember_sensitive(ctx, call_id)
        return [ev("tool_call", name, {"tool_name": p.get("name"), "arguments": args}, **kw)]
    if sub == "custom_tool_call":
        if mentions_sensitive_file(p.get("input")):
            _remember_sensitive(ctx, call_id)
        return [ev("tool_call", name, {"tool_name": p.get("name"), "input": p.get("input"),
                                       "status": p.get("status")}, **kw)]
    if sub in ("local_shell_call", "web_search_call"):
        if mentions_sensitive_file(p.get("action")):
            _remember_sensitive(ctx, call_id)
        return [ev("tool_call", name, {"tool_name": sub.removesuffix("_call"),
                                       "action": p.get("action"), "status": p.get("status")},
                   tool_use_id=call_id or p.get("id"), granularity="model_tool_call")]
    if sub in ("function_call_output", "custom_tool_call_output"):
        output, extra = p.get("output"), None
        outcome = detect_outcome(output)
        if call_id in ctx.sensitive_calls:
            output, extra = SENSITIVE_FILE_PLACEHOLDER, Counter(sensitive_file=1)
        return [ev("tool_result", name, {"output": output, "outcome": outcome},
                   extra_redactions=extra, **kw)]
    if sub == "reasoning":
        if p.get("encrypted_content"):
            stats["omitted:encrypted_reasoning"] += 1
        stats["duplicate:response_item/reasoning"] += 1
    elif sub == "message":
        stats["duplicate:response_item/message"] += 1
    else:
        stats[f"other:{name}"] += 1
    return []
