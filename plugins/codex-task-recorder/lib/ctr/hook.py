"""Turn one Codex lifecycle hook invocation into recorded events.

Contract with Codex: always exit 0, and print nothing except the single
`systemMessage` notice JSON. Plain stdout from some hooks becomes model
context, so the recorder must never print anything else.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter

from . import coverage, procinfo, state, store, timeutil, transcript
from .events import PARTIAL, UNAVAILABLE, detect_outcome, make_event
from .paths import Paths, safe_name
from .redact import SENSITIVE_FILE_PLACEHOLDER, mentions_sensitive_file, redact_text
from .sync import launcher
from .sync import state as syncstate
from .sync.config import SyncPaths

COMMON_FIELDS = {"session_id", "transcript_path", "cwd", "hook_event_name", "model",
                 "permission_mode", "turn_id"}
DOCUMENTED_FIELDS = {
    "SessionStart": ("source",),
    "SessionEnd": ("reason",),
    "UserPromptSubmit": ("turn_id", "prompt"),
    "PreToolUse": ("turn_id", "tool_name", "tool_use_id", "tool_input"),
    "PostToolUse": ("turn_id", "tool_name", "tool_use_id", "tool_input", "tool_response"),
    "PermissionRequest": ("turn_id", "tool_name", "tool_input"),
    "PreCompact": ("turn_id", "trigger"),
    "PostCompact": ("turn_id", "trigger"),
    "SubagentStart": ("turn_id", "agent_id", "agent_type"),
    "SubagentStop": ("turn_id", "agent_id", "agent_type", "agent_transcript_path",
                     "stop_hook_active", "last_assistant_message"),
    "Stop": ("turn_id", "stop_hook_active", "last_assistant_message"),
    "Interrupt": ("turn_id",),
}
STANDARD, BACKGROUND = "standard", "background"
INGEST_ON = {"UserPromptSubmit", "Stop", "Interrupt", "SessionEnd", "SubagentStop"}
SHORT_BUDGET_ON = {"Interrupt", "SessionEnd"}  # Codex caps these hooks at 3 s
LONG_BUDGET, SHORT_BUDGET = 8 * 1024 * 1024, 2 * 1024 * 1024

NOTICE_RECORDING = "● Codex Task Recorder 正在本地记录本会话（只保存在本机）。停止：双击「停止记录.cmd」"
NOTICE_SYNCING = ("● Codex Task Recorder 正在记录本会话，并按你的授权同步到后台。"
                  "暂停同步：双击「暂停同步.cmd」")
NOTICE_NOT_ENABLED = ("Codex Task Recorder 已安装，但尚未开启记录，不会记录本会话。"
                      "如需开启，请双击安装目录中的「开始记录.cmd」。")
# A host plugin (the CDK 充值 bundle) passes its own wording; None turns a notice off.
NOTICES = {"recording": NOTICE_RECORDING, "syncing": NOTICE_SYNCING, "not_enabled": NOTICE_NOT_ENABLED}


def main(stdin=None, stdout=None, paths: Paths | None = None, env=None) -> int:
    started = time.perf_counter()
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout
    env = os.environ if env is None else env
    paths = paths or Paths.default()
    name = "?"
    try:
        raw = stdin.read()
        payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        if not isinstance(payload, dict):
            return 0
        name = str(payload.get("hook_event_name") or "?")
        out = handle(payload, paths, env, started)
        if out:
            stdout.write(json.dumps(out, ensure_ascii=True))
            stdout.flush()
    except BaseException as exc:  # noqa: BLE001 - never break the user's session
        _log(paths, name, exc)
    return 0


def handle(payload: dict, paths: Paths, env, started: float, notices: dict | None = None) -> dict | None:
    if env.get("CTR_DISABLE") == "1":
        return None
    texts = {**NOTICES, **(notices or {})}
    name = str(payload.get("hook_event_name") or "Unknown")
    sid = payload.get("session_id") or ""
    st = state.load(paths.state)
    if st is None:  # never consented: record nothing, tell the user once per session
        if texts["not_enabled"] and name == "UserPromptSubmit" and sid and _first_notice(paths, sid):
            return {"systemMessage": texts["not_enabled"]}
        return None
    if not state.is_recording(st) or not state.cwd_in_scope(st, payload.get("cwd")):
        return None

    sid = sid or "unknown-session"
    sync_st = _sync_state(paths)
    out = None
    with store.session(paths, sid) as h:
        meta = h.cursor.setdefault("meta", {})
        kind = _session_kind(h.cursor, payload)
        events = hook_events(payload, meta)
        events += _track_model(payload, h.cursor, meta)
        if name in INGEST_ON:
            events += _ingest(payload, st, h.cursor, meta, name)
        notice = _notice(h.cursor, syncstate.is_on(sync_st), texts) if (
            name == "UserPromptSubmit" and kind == STANDARD) else None
        if notice:
            out = {"systemMessage": notice}
            events.append(make_event("recorder_notice", session_id=sid, source_type="derived",
                                     source_name="recorder", turn_id=payload.get("turn_id"),
                                     meta=meta, data={"channel": "systemMessage", "message": notice}))
        events.append(_invocation(payload, env, started, out is not None, meta))
        _stamp_model(events, h.cursor.get("hook_model"))
        _finish(events, h.cursor, kind, sid, meta)
        h.append(events)
    if syncstate.wants_worker(sync_st):
        try:
            launcher.kick(paths, sync_st)
        except Exception as exc:  # noqa: BLE001 - uploading is never worth breaking a hook
            _log(paths, name, exc)
    return out


def _sync_state(paths: Paths) -> syncstate.SyncState | None:
    try:
        return syncstate.load(SyncPaths(paths))
    except Exception:  # noqa: BLE001 - unreadable sync state means sync is off
        return None


def _notice(cursor: dict, syncing: bool, texts: dict = NOTICES) -> str | None:
    """The in-App notice, once per session; once more if sync is switched on mid-session."""
    if syncing and not cursor.get("sync_notice_shown"):
        cursor["sync_notice_shown"] = cursor["notice_shown"] = True
        return texts["syncing"]
    if not cursor.get("notice_shown"):
        cursor["notice_shown"] = True
        return texts["recording"]
    return None


def _session_kind(cursor: dict, payload: dict) -> str:
    """No transcript_path means Codex does not persist the thread: an App background task
    (e.g. the desktop App's suggestion generator), not a conversation the user typed."""
    has_transcript = bool(payload.get("transcript_path"))
    if cursor.get("session_kind") != STANDARD:
        cursor["session_kind"] = STANDARD if has_transcript else BACKGROUND
    return cursor["session_kind"]


def _stamp_kind(events: list[dict], kind: str) -> None:
    for e in events:
        e["session_kind"] = kind


def _stamp_model(events: list[dict], hook_model: str | None) -> None:
    """Hook and derived events take the session's current hook model. Transcript events keep
    the model of their own turn (set by the transcript reader), or null when it is unknown."""
    for e in events:
        if e["model"] is None and e["source"]["type"] != "transcript":
            e["model"] = hook_model


def _finish(events: list[dict], cursor: dict, kind: str, sid: str, meta: dict) -> None:
    """Add turn_coverage for turns that ended in this batch, then stamp the session kind."""
    events += coverage.update(cursor, events, kind == BACKGROUND, sid, meta)
    _stamp_kind(events, kind)


def hook_events(payload: dict, meta: dict) -> list[dict]:
    name = str(payload.get("hook_event_name") or "Unknown")
    sid = payload.get("session_id") or "unknown-session"
    documented = DOCUMENTED_FIELDS.get(name, ())
    missing = [f for f in documented if f not in payload]
    known = COMMON_FIELDS | set(documented) | {"description"}
    extra = {k: v for k, v in payload.items() if k not in known}
    g = payload.get

    def ev(kind: str, data: dict, **kw) -> dict:
        if extra:
            data = {**data, "_undocumented_fields": extra}
        kw.setdefault("missing", missing)
        return make_event(kind, session_id=sid, source_type="hook", source_name=name, data=data,
                          turn_id=g("turn_id"), meta=meta, **kw)

    if name == "SessionStart":
        return [ev("session_start", {"source": g("source"), "cwd": g("cwd"), "model": g("model"),
                                     "permission_mode": g("permission_mode")})]
    if name == "SessionEnd":
        return [ev("session_end", {"reason": g("reason")})]
    if name == "UserPromptSubmit":
        return [ev("user_prompt", {"text": g("prompt")})]
    if name == "PreToolUse":
        return [ev("tool_call", {"tool_name": g("tool_name"), "tool_input": g("tool_input")},
                   tool_use_id=g("tool_use_id"))]
    if name == "PostToolUse":
        response, extra_r = g("tool_response"), None
        if mentions_sensitive_file(g("tool_input")):
            response, extra_r = SENSITIVE_FILE_PLACEHOLDER, Counter(sensitive_file=1)
        return [ev("tool_result", {"tool_name": g("tool_name"), "tool_input": g("tool_input"),
                                   "tool_response": response,
                                   "outcome": detect_outcome(g("tool_response"))},
                   tool_use_id=g("tool_use_id"), extra_redactions=extra_r)]
    if name == "PermissionRequest":
        return [ev("permission_request", {"tool_name": g("tool_name"), "tool_input": g("tool_input"),
                                          "description": g("description")})]
    if name in ("PreCompact", "PostCompact"):
        return [ev("compaction", {"phase": "pre" if name == "PreCompact" else "post",
                                  "trigger": g("trigger")})]
    if name == "SubagentStart":
        return [ev("subagent_start", {"agent_type": g("agent_type")}, agent_id=g("agent_id"))]
    if name == "SubagentStop":
        return [ev("subagent_stop", {"agent_type": g("agent_type"),
                                     "agent_transcript_path": g("agent_transcript_path"),
                                     "stop_hook_active": g("stop_hook_active"),
                                     "last_assistant_message": g("last_assistant_message")},
                   agent_id=g("agent_id"))]
    if name == "Stop":
        message = g("last_assistant_message")
        return [ev("turn_end", {"stop_hook_active": g("stop_hook_active")}),
                ev("assistant_message", {"text": message, "scope": "last_message_only"},
                   missing=missing + ["intermediate_messages"],
                   status=UNAVAILABLE if message is None else PARTIAL,
                   note="Stop 只提供本回合最后一条 assistant 消息")]
    if name == "Interrupt":
        return [ev("turn_interrupted", {"permission_mode": g("permission_mode")})]
    return [ev("hook_unknown", {}, note="未在官方文档中列出的 Hook 事件")]


def _track_model(payload: dict, cursor: dict, meta: dict) -> list[dict]:
    model = payload.get("model")
    if not model:
        return []
    last = cursor.get("hook_model")
    cursor["hook_model"] = model
    common = {"session_id": payload.get("session_id") or "unknown-session",
              "turn_id": payload.get("turn_id"), "meta": meta}
    if last is None:
        return [make_event("model_config", source_type="hook",
                           source_name=str(payload.get("hook_event_name")),
                           data={"model": model, "permission_mode": payload.get("permission_mode"),
                                 "config_scope": "hook"},
                           missing=["reasoning_effort", "reasoning_summary", "approval_policy",
                                    "sandbox_policy"],
                           note="Hook 只提供模型名与权限模式；其余配置来自 transcript", **common)]
    if last != model:
        return [make_event("model_changed", source_type="derived", source_name="hook.model",
                           data={"from": last, "to": model}, **common)]
    return []


def _ingest(payload: dict, st: state.State, cursor: dict, meta: dict, name: str) -> list[dict]:
    budget = SHORT_BUDGET if name in SHORT_BUDGET_ON else LONG_BUDGET
    sid = payload.get("session_id") or "unknown-session"
    targets = []
    if payload.get("transcript_path"):
        targets.append((str(payload["transcript_path"]), None))
    if name == "SubagentStop" and payload.get("agent_transcript_path"):
        targets.append((str(payload["agent_transcript_path"]), payload.get("agent_id")))
    events: list[dict] = []
    for path, agent_id in targets:
        events += _ingest_path(path, agent_id, sid, st, cursor, meta, budget)
    return events


def _ingest_path(path: str, agent_id: str | None, sid: str, st: state.State, cursor: dict,
                 meta: dict, budget: int) -> list[dict]:
    sensitive = cursor.setdefault("sensitive_calls", [])
    tcur = cursor.setdefault("transcripts", {}).setdefault(path, {"offset": 0})
    if agent_id:
        tcur["agent_id"] = agent_id
    ctx = transcript.Context(session_id=sid, meta=meta, sensitive_calls=sensitive,
                             agent_id=agent_id, last_model=tcur.get("last_model"),
                             current_turn=tcur.get("current_turn"))
    result = transcript.ingest(path, tcur["offset"], st, ctx, budget)
    tcur.update(offset=result.offset, last_model=ctx.last_model, current_turn=ctx.current_turn)
    events = list(result.events)
    if result.stats:
        events.append(make_event(
            "transcript_other", session_id=sid, source_type="transcript", source_name="summary",
            meta=meta, agent_id=agent_id, data={"counts": dict(result.stats)},
            note="未映射或按策略省略的 transcript 记录：只计类型，不存内容"))
    return events


def flush_transcripts(paths: Paths, st: state.State) -> int:
    """Read transcript lines written after a session's last hook (e.g. turn_aborted after
    Interrupt, or a final turn whose async Stop never ran). Consent windows still apply."""
    if not paths.cursors.exists():
        return 0
    added = 0
    for cursor_file in sorted(paths.cursors.glob("*.json")):
        try:
            sid = json.loads(cursor_file.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError):
            continue
        if not sid:
            continue
        with store.session(paths, sid) as h:
            meta = h.cursor.setdefault("meta", {})
            events: list[dict] = []
            for path, tcur in list(h.cursor.get("transcripts", {}).items()):
                events += _ingest_path(path, tcur.get("agent_id"), sid, st, h.cursor, meta, LONG_BUDGET)
            _stamp_model(events, h.cursor.get("hook_model"))
            _finish(events, h.cursor, h.cursor.get("session_kind", STANDARD), sid, meta)
            h.append(events)
            added += len(events)
    return added


def _invocation(payload: dict, env, started: float, notice: bool, meta: dict) -> dict:
    name = str(payload.get("hook_event_name") or "Unknown")
    return make_event("hook_invocation", session_id=payload.get("session_id") or "unknown-session",
                      source_type="hook", source_name=name, turn_id=payload.get("turn_id"), meta=meta,
                      data={
                          "payload_keys": sorted(payload.keys()),
                          "transcript_path_present": bool(payload.get("transcript_path")),
                          "env": {"PLUGIN_ROOT": env.get("PLUGIN_ROOT"),
                                  "PLUGIN_DATA": env.get("PLUGIN_DATA"),
                                  "CLAUDE_PLUGIN_ROOT_set": "CLAUDE_PLUGIN_ROOT" in env,
                                  "ComSpec": env.get("ComSpec") or env.get("COMSPEC")},
                          "process_chain": procinfo.parent_chain(),
                          "python": sys.version.split()[0],
                          "python_executable": sys.executable,
                          "hook_script": sys.argv[0] if sys.argv else None,
                          "process_cwd": os.getcwd(),
                          "notice_emitted": notice,
                          "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                      })


def _first_notice(paths: Paths, sid: str) -> bool:
    """Marker file (no content) so the 'not enabled' notice shows once per session."""
    marker = paths.notices / safe_name(sid)
    if marker.exists():
        return False
    paths.notices.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return True


def _log(paths: Paths, name: str, exc: BaseException) -> None:
    try:
        paths.root.mkdir(parents=True, exist_ok=True)
        message, _ = redact_text(str(exc))
        line = json.dumps({"at": timeutil.iso(timeutil.now()), "event": name,
                           "error": type(exc).__name__, "message": message[:200]})
        with open(paths.log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except BaseException:  # noqa: BLE001
        pass
