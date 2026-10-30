"""Turn one rollout JSONL line into csr.event/v1 events.

Only record types observed in Codex 0.163.0-alpha.10.2 rollouts are mapped (see the
session-reader spec, section 2). Duplicates, policy-omitted records and unknown types are
counted by name and never stored. Nothing here decrypts, guesses or back-fills a value:
what the record does not contain stays null with a reason in `missing`.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field

from ctr.redact import SENSITIVE_FILE_PLACEHOLDER, mentions_sensitive_file, redact_obj

SCHEMA = "csr.event/v1"
MAX_FIELD_CHARS = 256 * 1024
MAX_TURN_MODELS = 64
MAX_SENSITIVE_CALLS = 200

# Events that belong to a turn even when the record itself carries no turn_id.
TURN_SCOPED = {"user_prompt", "assistant_message", "reasoning_summary", "tool_call", "tool_result",
               "tool_execution", "token_usage", "turn_error"}

NO_TURN = "记录无 turn_id，且当时没有进行中的回合"
NO_TS = "记录缺少 timestamp"
NO_SUMMARY = "模型未返回公开推理摘要（加密推理内容不解密）"


@dataclass
class Context:
    """Parser state carried across lines and saved with the read offset."""
    session_id: str
    codex_version: str | None = None
    thread_model: str | None = None
    turn_id: str | None = None
    turn_models: dict = field(default_factory=dict)
    last_turn_model: str | None = None
    last_ordinal: int = -1
    bad_since_last: int = 0
    sensitive_calls: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Context":
        return cls(**json.loads(text))


@dataclass
class Issue:
    category: str  # parse | missing | order | file
    code: str
    detail: str
    offset: int | None = None
    ordinal: int | None = None
    count: int = 1


@dataclass
class Parsed:
    events: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


@dataclass
class _Rec:
    ordinal: int | None
    ts: str | None
    offset: int
    name: str
    file_name: str


def parse_line(raw: bytes, offset: int, ctx: Context, file_name: str) -> Parsed:
    out = Parsed()
    try:
        rec = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        ctx.bad_since_last += 1
        out.issues.append(Issue("parse", "bad_encoding", f"不是 UTF-8，行长 {len(raw)} 字节", offset))
        return out
    except ValueError as exc:
        ctx.bad_since_last += 1
        where = f"第 {exc.colno} 列，" if isinstance(exc, json.JSONDecodeError) else ""
        msg = exc.msg if isinstance(exc, json.JSONDecodeError) else type(exc).__name__
        out.issues.append(Issue("parse", "bad_json", f"JSON 解析失败：{msg}（{where}行长 {len(raw)} 字节）",
                                offset))
        return out
    if not isinstance(rec, dict):
        ctx.bad_since_last += 1
        out.issues.append(Issue("parse", "not_object", f"顶层是 {type(rec).__name__}，不是对象", offset))
        return out

    ordinal = rec.get("ordinal") if isinstance(rec.get("ordinal"), int) else None
    if ordinal is not None:
        _track_ordinal(ordinal, offset, ctx, out)

    rtype = rec.get("type")
    if not isinstance(rtype, str):
        out.issues.append(Issue("parse", "missing_type", "记录缺少字符串类型的 type", offset, ordinal))
        return out
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        out.issues.append(Issue("parse", "bad_payload", f"{rtype} 的 payload 不是对象", offset, ordinal))
        return out
    ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
    sub = payload.get("type") if isinstance(payload.get("type"), str) else None
    name = f"{rtype}/{sub}" if sub else rtype
    r = _Rec(ordinal, ts, offset, name, file_name)

    if rtype == "session_meta":
        _session_meta(payload, r, ctx, out)
    elif rtype == "turn_context":
        _turn_context(payload, r, ctx, out)
    elif rtype == "event_msg":
        _event_msg(sub, payload, r, ctx, out)
    elif rtype == "response_item":
        _response_item(sub, payload, r, ctx, out)
    elif rtype == "token_usage_record":
        _emit(out, ctx, r, "token_usage", {
            "response_id": payload.get("response_id"), "usage": payload.get("usage"),
            "turn_token_usage": payload.get("turn_token_usage"),
            "thread_token_usage": payload.get("thread_token_usage")}, turn_id=payload.get("turn_id"))
    elif rtype == "world_state":
        out.skipped["omitted:world_state"] += 1
    else:
        out.skipped[f"unrecognized:{name}"] += 1
    return out


def _track_ordinal(ordinal: int, offset: int, ctx: Context, out: Parsed) -> None:
    expected = ctx.last_ordinal + 1
    if ordinal > expected:
        gap = ordinal - expected - ctx.bad_since_last  # unreadable lines already counted as parse failures
        if gap > 0:
            out.issues.append(Issue("missing", "ordinal_gap",
                                    f"缺少 {gap} 条记录（ordinal {expected}–{ordinal - 1} 之间）",
                                    offset, ordinal, gap))
    elif ordinal < expected:
        out.issues.append(Issue("order", "ordinal_repeat",
                                f"ordinal {ordinal} 不大于上一条 {ctx.last_ordinal}（重复或乱序）",
                                offset, ordinal))
    ctx.last_ordinal = max(ctx.last_ordinal, ordinal)
    ctx.bad_since_last = 0


def _sub(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, dict) else None


def _duration_ms(value: object) -> int | None:
    if isinstance(value, dict) and isinstance(value.get("secs"), int):
        return value["secs"] * 1000 + int(value.get("nanos") or 0) // 1_000_000
    return None


def _text_parts(content: object) -> tuple[str | None, list[str]]:
    if isinstance(content, str):
        return content, []
    texts, other = [], []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif isinstance(part, dict):
            other.append(str(part.get("type")))
    return ("\n".join(texts) if texts else None), other


def _outcome(*, exit_code: object = None, status: object = None, error: object = None,
             is_error: object = None) -> str:
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return "success" if exit_code == 0 else "error"
    if error or is_error is True:
        return "error"
    if isinstance(status, str):
        s = status.lower()
        if s in ("failed", "error", "declined", "rejected", "cancelled", "canceled", "timed_out"):
            return "error"
        if s in ("completed", "success", "succeeded", "ok"):
            return "success"
    return "unknown"


def _output_outcome(output: object) -> str:
    """custom/function call outputs are text such as "Exit code: 1\\n…"; anything else is unknown."""
    if isinstance(output, str):
        head = output.lstrip()[:200].lower()
        for prefix in ("exit code:", "process exited with code"):
            if head.startswith(prefix):
                code = head[len(prefix):].split()[0] if head[len(prefix):].split() else ""
                if code.lstrip("-").isdigit():
                    return "success" if int(code) == 0 else "error"
    return "unknown"


def _truncate(value: object, path: str, notes: list) -> object:
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


def _emit(out: Parsed, ctx: Context, r: _Rec, kind: str, data: dict, *, turn_id: object = None,
          item_id: object = None, call_id: object = None, missing: list | None = None,
          sensitive: bool = False) -> None:
    missing = list(missing or [])
    inferred = False
    if not isinstance(turn_id, str) or not turn_id:
        turn_id = None
        if kind in TURN_SCOPED:
            if ctx.turn_id:
                turn_id, inferred = ctx.turn_id, True
            else:
                missing.append({"field": "turn_id", "reason": NO_TURN})
    if r.ts is None:
        missing.append({"field": "ts", "reason": NO_TS})
    redacted, counts = redact_obj(data)
    if sensitive:
        counts["sensitive_file"] += 1
    truncated: list = []
    redacted = _truncate(redacted, "data", truncated)
    index = len(out.events)
    key_pos = str(r.ordinal) if r.ordinal is not None else f"@{r.offset}"
    model = None
    if kind != "session_meta":
        model = ctx.turn_models.get(turn_id) if turn_id else None
        model = model or ctx.thread_model
    out.events.append({
        "schema": SCHEMA,
        "event_key": f"{ctx.session_id}:{key_pos}:{index}",
        "session_id": ctx.session_id,
        "turn_id": turn_id,
        "turn_id_inferred": inferred,
        "ordinal": r.ordinal,
        "ts": r.ts,
        "kind": kind,
        "model": model,
        "item_id": item_id if isinstance(item_id, str) else None,
        "call_id": call_id if isinstance(call_id, str) else None,
        "data": redacted,
        "missing": missing,
        "source": {"file": r.file_name, "offset": r.offset, "record": r.name,
                   "codex_version": ctx.codex_version},
        "redactions": dict(counts),
        "truncated": truncated,
    })


def _session_meta(p: dict, r: _Rec, ctx: Context, out: Parsed) -> None:
    sid = p.get("id") if isinstance(p.get("id"), str) else p.get("session_id")
    if isinstance(sid, str) and sid and sid != ctx.session_id:
        out.issues.append(Issue("file", "session_id_mismatch",
                                f"文件名会话 ID {ctx.session_id} 与 session_meta.id {sid} 不一致，以后者为准",
                                r.offset, r.ordinal))
        ctx.session_id = sid
    if isinstance(p.get("cli_version"), str):
        ctx.codex_version = p["cli_version"]
    if "creator_user_id" in p or "creator_account_id" in p:
        out.skipped["omitted:session_meta.creator_ids"] += 1
    base = p.get("base_instructions")
    _emit(out, ctx, r, "session_meta", {
        "session_id": sid, "cli_version": p.get("cli_version"), "originator": p.get("originator"),
        "source": p.get("source"), "thread_source": p.get("thread_source"), "cwd": p.get("cwd"),
        "model_provider": p.get("model_provider"), "context_window": p.get("context_window"),
        "history_mode": p.get("history_mode"),
        "base_instructions_length": len(base) if isinstance(base, str) else None})


def _model_config(out: Parsed, ctx: Context, r: _Rec, scope: str, turn_id: object, data: dict) -> None:
    data = {"scope": scope, **data}
    reason = f"{r.name} 未提供该字段"
    missing = [{"field": k, "reason": reason} for k in ("model", "reasoning_effort") if data.get(k) is None]
    _emit(out, ctx, r, "model_config", data, turn_id=turn_id, missing=missing)


def _turn_context(p: dict, r: _Rec, ctx: Context, out: Parsed) -> None:
    turn_id = p.get("turn_id") if isinstance(p.get("turn_id"), str) else None
    model = p.get("model") if isinstance(p.get("model"), str) else None
    if turn_id:
        ctx.turn_id = turn_id
        if model:
            ctx.turn_models[turn_id] = model
            while len(ctx.turn_models) > MAX_TURN_MODELS:
                ctx.turn_models.pop(next(iter(ctx.turn_models)))
    if model:
        ctx.thread_model = model
    _model_config(out, ctx, r, "turn", turn_id, {
        "model": model, "reasoning_effort": p.get("effort"), "reasoning_summary": p.get("summary"),
        "approval_policy": p.get("approval_policy"), "approvals_reviewer": p.get("approvals_reviewer"),
        "sandbox_policy": _sub(p.get("sandbox_policy"), "type"),
        "network_access": _sub(p.get("sandbox_policy"), "network_access"),
        "collaboration_mode": _sub(p.get("collaboration_mode"), "mode"),
        "permission_profile": _sub(p.get("permission_profile"), "type"), "cwd": p.get("cwd")})
    if model and ctx.last_turn_model and model != ctx.last_turn_model:
        _emit(out, ctx, r, "model_changed", {"from": ctx.last_turn_model, "to": model}, turn_id=turn_id)
    if model:
        ctx.last_turn_model = model


def _end_turn(ctx: Context, turn_id: object) -> None:
    if turn_id and ctx.turn_id == turn_id:
        ctx.turn_id = None


def _event_msg(sub: str | None, p: dict, r: _Rec, ctx: Context, out: Parsed) -> None:
    turn_id = p.get("turn_id")
    if sub == "thread_settings_applied":
        s = p.get("thread_settings") if isinstance(p.get("thread_settings"), dict) else {}
        if isinstance(s.get("model"), str):
            ctx.thread_model = s["model"]
        _model_config(out, ctx, r, "thread", None, {
            "model": s.get("model"), "reasoning_effort": s.get("reasoning_effort"),
            "reasoning_summary": s.get("reasoning_summary"), "model_provider": s.get("model_provider_id"),
            "service_tier": s.get("service_tier"), "approval_policy": s.get("approval_policy"),
            "approvals_reviewer": s.get("approvals_reviewer"), "personality": s.get("personality"),
            "collaboration_mode": _sub(s.get("collaboration_mode"), "mode"), "cwd": s.get("cwd")})
    elif sub == "task_started":
        if isinstance(turn_id, str) and turn_id:
            ctx.turn_id = turn_id
        attr = p.get("turn_attribution")
        _emit(out, ctx, r, "turn_started", {
            "started_at": p.get("started_at"), "model_context_window": p.get("model_context_window"),
            "collaboration_mode_kind": p.get("collaboration_mode_kind"),
            "turn_trigger": _sub(attr, "turn_trigger"), "parent_turn_id": _sub(attr, "parent_turn_id")},
            turn_id=turn_id)
    elif sub == "task_complete":
        err = p.get("error")
        missing = []
        if p.get("time_to_first_token_ms") is None:
            missing.append({"field": "time_to_first_token_ms", "reason": "记录未提供（出错或无输出的回合常见）"})
        _emit(out, ctx, r, "turn_completed", {
            "status": "error" if err else "completed", "started_at": p.get("started_at"),
            "completed_at": p.get("completed_at"), "duration_ms": p.get("duration_ms"),
            "time_to_first_token_ms": p.get("time_to_first_token_ms"),
            "last_agent_message": p.get("last_agent_message")}, turn_id=turn_id, missing=missing)
        if err:
            _emit(out, ctx, r, "turn_error", {"origin": "task_complete", "message": _sub(err, "message"),
                                              "codex_error_info": _sub(err, "codex_error_info")},
                  turn_id=turn_id)
        _end_turn(ctx, turn_id)
    elif sub == "turn_aborted":
        _emit(out, ctx, r, "turn_aborted", {
            "reason": p.get("reason"), "started_at": p.get("started_at"),
            "completed_at": p.get("completed_at"), "duration_ms": p.get("duration_ms")}, turn_id=turn_id)
        _end_turn(ctx, turn_id)
    elif sub == "error":
        _emit(out, ctx, r, "turn_error", {"origin": "error", "message": p.get("message"),
                                          "codex_error_info": p.get("codex_error_info")}, turn_id=turn_id)
    elif sub == "token_count":
        out.skipped["duplicate:event_msg/token_count"] += 1
        if p.get("rate_limits") is not None:
            out.skipped["omitted:rate_limits"] += 1
    elif sub == "item_completed":
        _item(p, r, ctx, out)
    else:
        out.skipped[f"unrecognized:{r.name}"] += 1


def _remember_sensitive(ctx: Context, call_id: object) -> None:
    if isinstance(call_id, str) and call_id not in ctx.sensitive_calls:
        ctx.sensitive_calls.append(call_id)
        del ctx.sensitive_calls[:-MAX_SENSITIVE_CALLS]


def _item(p: dict, r: _Rec, ctx: Context, out: Parsed) -> None:
    it = p.get("item") if isinstance(p.get("item"), dict) else {}
    itype = it.get("type")
    r.name = f"event_msg/item_completed:{itype}"
    kw = {"turn_id": p.get("turn_id"), "item_id": it.get("id")}
    timing = {"started_at_ms": p.get("started_at_ms"), "completed_at_ms": p.get("completed_at_ms")}

    if itype in ("UserMessage", "AgentMessage"):
        text, other = _text_parts(it.get("content"))
        missing = [] if text is not None else [
            {"field": "text", "reason": "消息没有文本部分" + ("（含非文本输入，不展开）" if other else "")}]
        data = {"text": text, "non_text_parts": other, **timing}
        if itype == "AgentMessage":
            data = {"text": text, "phase": it.get("phase"), "non_text_parts": other, **timing}
        _emit(out, ctx, r, "user_prompt" if itype == "UserMessage" else "assistant_message", data,
              missing=missing, **kw)
    elif itype == "Reasoning":
        summary = it.get("summary_text")
        text = "\n".join(s for s in summary if isinstance(s, str)) if isinstance(summary, list) else summary
        text = text if isinstance(text, str) and text else None
        if it.get("raw_content"):
            out.skipped["omitted:reasoning_raw_content"] += 1
        _emit(out, ctx, r, "reasoning_summary", {"summary": text, **timing},
              missing=[] if text else [{"field": "summary", "reason": NO_SUMMARY}], **kw)
    elif itype == "CommandExecution":
        command, output, sensitive = it.get("command"), it.get("aggregated_output"), False
        if mentions_sensitive_file(command):
            output, sensitive = SENSITIVE_FILE_PLACEHOLDER, True
        _emit(out, ctx, r, "tool_execution", {
            "tool_name": "CommandExecution", "command": command, "cwd": it.get("cwd"),
            "exit_code": it.get("exit_code"), "status": it.get("status"),
            "duration_ms": _duration_ms(it.get("duration")), "source": it.get("source"), "output": output,
            "outcome": _outcome(exit_code=it.get("exit_code"), status=it.get("status")), **timing},
            sensitive=sensitive, **kw)
    elif itype == "McpToolCall":
        args, result, sensitive = it.get("arguments"), it.get("result"), False
        if mentions_sensitive_file(args):
            result, sensitive = SENSITIVE_FILE_PLACEHOLDER, True
        _emit(out, ctx, r, "tool_execution", {
            "tool_name": f"mcp:{it.get('server')}/{it.get('tool')}", "server": it.get("server"),
            "tool": it.get("tool"), "arguments": args, "result": result, "error": it.get("error"),
            "status": it.get("status"), "duration_ms": _duration_ms(it.get("duration")),
            "outcome": _outcome(status=it.get("status"), error=it.get("error"),
                                is_error=_sub(it.get("result"), "isError")), **timing},
            sensitive=sensitive, **kw)
    elif itype == "FileChange":
        _emit(out, ctx, r, "tool_execution", {
            "tool_name": "FileChange", "changes": it.get("changes"), "status": it.get("status"),
            "outcome": _outcome(status=it.get("status")), **timing}, **kw)
    else:
        out.skipped[f"unrecognized:{r.name}"] += 1


def _response_item(sub: str | None, p: dict, r: _Rec, ctx: Context, out: Parsed) -> None:
    call_id = p.get("call_id")
    if sub in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
        if sub == "function_call":
            args = p.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                pass
            data, probe = {"tool_name": p.get("name"), "arguments": args}, args
        elif sub == "custom_tool_call":
            data, probe = {"tool_name": p.get("name"), "input": p.get("input")}, p.get("input")
        else:
            call_id = call_id or p.get("id")
            data, probe = {"tool_name": sub.removesuffix("_call"), "action": p.get("action")}, p.get("action")
        data["status"] = p.get("status")
        if mentions_sensitive_file(probe):
            _remember_sensitive(ctx, call_id)
        missing = [] if call_id else [{"field": "call_id", "reason": f"{r.name} 未提供 call_id，无法关联结果"}]
        _emit(out, ctx, r, "tool_call", data, call_id=call_id, missing=missing)
    elif sub in ("function_call_output", "custom_tool_call_output"):
        output, sensitive = p.get("output"), False
        outcome = _output_outcome(output)
        if call_id in ctx.sensitive_calls:
            output, sensitive = SENSITIVE_FILE_PLACEHOLDER, True
        _emit(out, ctx, r, "tool_result", {"output": output, "outcome": outcome}, call_id=call_id,
              sensitive=sensitive)
    elif sub == "message":
        out.skipped["duplicate:response_item/message"] += 1
    elif sub == "reasoning":
        out.skipped["duplicate:response_item/reasoning"] += 1
        if p.get("encrypted_content"):
            out.skipped["omitted:encrypted_reasoning"] += 1
    elif sub == "additional_tools":
        out.skipped["omitted:response_item/additional_tools"] += 1
    else:
        out.skipped[f"unrecognized:{r.name}"] += 1
