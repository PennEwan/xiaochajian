"""Session statistics and field coverage, computed from stored events (never prints content)."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .parser import TURN_SCOPED
from .storage import Storage

PARSE_CODES = {"bad_json", "bad_encoding", "not_object", "missing_type", "bad_payload"}


def current_session(storage: Storage) -> str | None:
    row = storage.conn.execute("SELECT session_id FROM files ORDER BY updated_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _turn_status(events: list[dict]) -> dict:
    status: dict[str, str] = {}
    for e in events:
        tid = e["turn_id"]
        if not tid:
            continue
        if e["kind"] == "turn_started":
            status.setdefault(tid, "open")
        elif e["kind"] == "turn_completed":
            status[tid] = "failed" if e["data"].get("status") == "error" else "completed"
        elif e["kind"] == "turn_aborted":
            status[tid] = "aborted"
    return status


def _turn_models(events: list[dict]) -> dict:
    return {e["turn_id"]: e["data"] for e in events
            if e["kind"] == "model_config" and e["data"].get("scope") == "turn" and e["turn_id"]}


def session_stats(storage: Storage, session_id: str) -> dict:
    events = list(storage.iter_events([session_id]))
    kinds = Counter(e["kind"] for e in events)
    by_kind: dict[str, list] = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e)
    meta = (by_kind.get("session_meta") or [{"data": {}}])[0]["data"]

    turn_status = _turn_status(events)
    states = Counter(turn_status.values())
    started = {e["turn_id"] for e in by_kind.get("turn_started", []) if e["turn_id"]}

    models: dict[tuple, int] = {}
    for cfg in _turn_models(events).values():
        k = (cfg.get("model"), cfg.get("reasoning_effort"))
        models[k] = models.get(k, 0) + 1

    phases = Counter(e["data"].get("phase") for e in by_kind.get("assistant_message", []))
    calls = by_kind.get("tool_call", [])
    answered = {e["call_id"] for e in by_kind.get("tool_result", []) if e["call_id"]}
    unanswered = sum(1 for c in calls if not c["call_id"] or c["call_id"] not in answered)
    execs = by_kind.get("tool_execution", [])

    reasoning = by_kind.get("reasoning_summary", [])
    public = sum(1 for e in reasoning if e["data"].get("summary"))
    tokens = None
    for e in by_kind.get("token_usage", []):
        if isinstance(e["data"].get("thread_token_usage"), dict):
            tokens = e["data"]["thread_token_usage"]

    issues = storage.issues(session_id)
    issue_codes = Counter()
    for i in issues:
        issue_codes[i["code"]] += i["count"]
    missing_fields = Counter(m["field"] for e in events for m in e["missing"])
    timestamps = sorted(e["ts"] for e in events if e["ts"])

    return {
        "session_id": session_id,
        "files": [{"name": Path(f.path).name, "path": f.path, "offset": f.offset, "size": f.size,
                   "pending": f.pending} for f in storage.files(session_id)],
        "codex_version": meta.get("cli_version"),
        "originator": meta.get("originator"),
        "first_ts": timestamps[0] if timestamps else None,
        "last_ts": timestamps[-1] if timestamps else None,
        "events": len(events),
        "prompts": kinds["user_prompt"],
        "turns": {"started": len(started), "completed": states["completed"], "failed": states["failed"],
                  "aborted": states["aborted"], "open": states["open"]},
        "models": [{"model": m, "reasoning_effort": eff, "turns": n} for (m, eff), n in models.items()],
        "model_switches": kinds["model_changed"],
        "replies": {"total": kinds["assistant_message"], "final_answer": phases["final_answer"],
                    "commentary": phases["commentary"],
                    "other": kinds["assistant_message"] - phases["final_answer"] - phases["commentary"]},
        "tool_calls": {"model_level": len(calls), "with_result": len(calls) - unanswered,
                       "without_result": unanswered, "executions": len(execs),
                       "execution_types": dict(Counter(e["data"].get("tool_name") for e in execs)),
                       "execution_failed": sum(1 for e in execs if e["data"].get("outcome") == "error")},
        "reasoning": {"public": public, "unavailable": len(reasoning) - public},
        "tokens": tokens,
        "errors": kinds["turn_error"],
        "parse_failures": sum(n for c, n in issue_codes.items() if c in PARSE_CODES),
        "missing": {"ordinal_gap_records": issue_codes["ordinal_gap"],
                    "tool_calls_without_result": unanswered, "open_turns": states["open"],
                    "events_with_missing_fields": sum(1 for e in events if e["missing"]),
                    "missing_fields": dict(missing_fields)},
        "other_issues": {c: n for c, n in issue_codes.items() if c not in PARSE_CODES and c != "ordinal_gap"},
        "skipped": dict(sorted(storage.skipped(session_id).items())),
    }


def _row(field: str, source: str, applicable: int, present: int, note: str = "") -> dict:
    return {"field": field, "source": source, "applicable": applicable, "present": present, "note": note}


def coverage(storage: Storage, session_ids: list[str]) -> list[dict]:
    events = list(storage.iter_events(session_ids))
    by_kind: dict[str, list] = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e)
    k = lambda name: by_kind.get(name, [])  # noqa: E731

    scoped = [e for e in events if e["kind"] in TURN_SCOPED or e["kind"].startswith("turn_")]
    inferred = sum(1 for e in scoped if e["turn_id_inferred"])
    turn_cfg = [e for e in k("model_config") if e["data"].get("scope") == "turn"]
    turns = {e["turn_id"] for e in k("turn_started") if e["turn_id"]}
    turn_models = _turn_models(events)
    ended = {e["turn_id"] for e in k("turn_completed") + k("turn_aborted")}
    completed = {e["turn_id"] for e in k("turn_completed")}
    with_tokens = {e["turn_id"] for e in k("token_usage")}
    answered = {e["call_id"] for e in k("tool_result") if e["call_id"]}
    calls = k("tool_call")
    skipped = Counter()
    for sid in session_ids:
        skipped.update(storage.skipped(sid))

    def has(e: dict, field: str) -> bool:
        return e["data"].get(field) not in (None, "", [])

    return [
        _row("Session ID", "session_meta.id / 文件名", len(events), sum(1 for e in events if e["session_id"])),
        _row("Turn ID", "各记录 turn_id", len(scoped), sum(1 for e in scoped if e["turn_id"]),
             f"其中 {inferred} 条按当前回合推断（模型级工具调用记录本身不带 turn_id）"),
        _row("时间戳", "每行 timestamp", len(events), sum(1 for e in events if e["ts"])),
        _row("用户 Prompt", "item_completed:UserMessage", len(k("user_prompt")),
             sum(1 for e in k("user_prompt") if has(e, "text"))),
        _row("模型公开消息", "item_completed:AgentMessage", len(k("assistant_message")),
             sum(1 for e in k("assistant_message") if has(e, "text")), "phase 区分 commentary / final_answer"),
        _row("模型名称", "turn_context.model（按回合）", len(turns),
             sum(1 for t in turns if turn_models.get(t, {}).get("model"))),
        _row("推理强度", "turn_context.effort", len(turn_cfg), sum(1 for e in turn_cfg if has(e, "reasoning_effort"))),
        _row("推理摘要设置", "turn_context.summary", len(turn_cfg),
             sum(1 for e in turn_cfg if has(e, "reasoning_summary"))),
        _row("工具调用名称", "custom_tool_call / function_call", len(calls), sum(1 for e in calls if has(e, "tool_name"))),
        _row("工具调用参数", "同上 input / arguments", len(calls),
             sum(1 for e in calls if has(e, "input") or has(e, "arguments") or has(e, "action"))),
        _row("工具调用结果", "*_call_output（按 call_id 关联）", len(calls),
             sum(1 for e in calls if e["call_id"] in answered)),
        _row("工具执行结果（退出码 / 状态）", "item_completed:CommandExecution / McpToolCall", len(k("tool_execution")),
             sum(1 for e in k("tool_execution") if e["data"].get("outcome") != "unknown"),
             "执行条目与模型级调用之间没有关联 ID"),
        _row("公开推理摘要", "item_completed:Reasoning.summary_text", len(k("reasoning_summary")),
             sum(1 for e in k("reasoning_summary") if has(e, "summary")),
             f"加密推理 {skipped['omitted:encrypted_reasoning']} 条未解密"),
        _row("回合结束事件", "task_complete / turn_aborted", len(turns), len(turns & ended),
             f"出错 {len(k('turn_error'))}，中断 {len(k('turn_aborted'))}"),
        _row("Token 使用量", "token_usage_record", len(completed), len(completed & with_tokens),
             "被中断的回合没有 token_usage_record"),
        _row("首 Token 时延", "task_complete.time_to_first_token_ms", len(k("turn_completed")),
             sum(1 for e in k("turn_completed") if e["data"].get("time_to_first_token_ms") is not None)),
    ]


def _pct(present: int, applicable: int) -> str:
    return f"{present / applicable:.0%}" if applicable else "—"


def coverage_markdown(rows: list[dict]) -> str:
    out = ["| 字段 | 来源记录 | 适用数 | 有值 | 覆盖率 | 说明 |", "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['field']} | {r['source']} | {r['applicable']} | {r['present']} | "
                   f"{_pct(r['present'], r['applicable'])} | {r['note']} |")
    return "\n".join(out)
