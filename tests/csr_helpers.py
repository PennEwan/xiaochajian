"""Synthetic rollout files shaped like Codex 0.163.0-alpha.10.2 (see the session-reader spec, section 2)."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SID = "01a18266-5a69-7a92-a786-be8e4f80663f"
SID2 = "01a1824f-7779-7b63-85c9-ef95d77918de"
T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
VERSION = "0.163.0-alpha.10.2"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def file_name(sid: str = SID) -> str:
    return f"rollout-2026-10-28T20-05-08-{sid}.jsonl"


def usage(total: int) -> dict:
    return {"input_tokens": total - 100, "cached_input_tokens": total // 2, "output_tokens": 100,
            "reasoning_output_tokens": 40, "total_tokens": total, "cache_write_input_tokens": 0}


class Rollout:
    """Builds records with consecutive ordinals, the way Codex writes them."""

    def __init__(self, sid: str = SID):
        self.sid = sid
        self.records: list[dict] = []
        self.ordinal = 0
        self.clock = T0
        self.thread_tokens = 0
        self._call = 0

    def add(self, rtype: str, payload: dict, metadata: dict | None = None) -> dict:
        self.clock += timedelta(milliseconds=250)
        r = {"timestamp": iso(self.clock), "ordinal": self.ordinal, "type": rtype, "payload": payload}
        if metadata is not None:
            r["metadata"] = metadata
        self.ordinal += 1
        self.records.append(r)
        return r

    def meta(self, originator: str = "Codex Desktop") -> "Rollout":
        self.add("session_meta", {
            "id": self.sid, "session_id": self.sid, "timestamp": iso(T0), "cwd": "D:\\ctr-testbed",
            "originator": originator, "cli_version": VERSION, "source": "vscode",
            "model_provider": "openai", "context_window": 272000, "history_mode": "full",
            "base_instructions": "SYSTEM PROMPT TEXT", "creator_user_id": "user-SECRETUSER",
            "creator_account_id": "acct-SECRETACCOUNT", "runtime_workspace_roots": ["D:\\ctr-testbed"]})
        return self

    def thread_settings(self, model: str, effort: str = "high") -> "Rollout":
        self.add("event_msg", {"type": "thread_settings_applied", "thread_id": self.sid, "thread_settings": {
            "model": model, "model_provider_id": "openai", "reasoning_effort": effort,
            "service_tier": "default", "approval_policy": "on-request", "approvals_reviewer": "user",
            "collaboration_mode": {"mode": "default", "settings": {"model": model}}, "cwd": "D:\\ctr-testbed"}})
        return self

    def item(self, turn: str, it: dict) -> dict:
        return self.add("event_msg", {"type": "item_completed", "thread_id": self.sid, "turn_id": turn,
                                      "started_at_ms": 1, "completed_at_ms": 2, "item": it})

    def start(self, turn: str, model: str = "gpt-6-luna", effort: str = "xhigh") -> "Rollout":
        self.add("event_msg", {"type": "task_started", "turn_id": turn, "root_turn_id": turn,
                               "started_at": 1000, "model_context_window": 272000,
                               "collaboration_mode_kind": "default",
                               "turn_attribution": {"turn_id": turn, "root_turn_id": turn,
                                                    "parent_turn_id": None, "turn_trigger": None,
                                                    "initiating_agent_path": None}})
        self.add("world_state", {"full": True, "state": {"base_instructions": "SYSTEM"}})
        self.add("turn_context", {"turn_id": turn, "root_turn_id": turn, "model": model, "effort": effort,
                                  "summary": "none", "approval_policy": "on-request",
                                  "approvals_reviewer": "user", "cwd": "D:\\ctr-testbed",
                                  "sandbox_policy": {"type": "workspace-write", "network_access": False},
                                  "collaboration_mode": {"mode": "default"},
                                  "permission_profile": {"type": "managed"}})
        return self

    def prompt(self, turn: str, text: str) -> "Rollout":
        self.add("response_item", {"type": "message", "role": "user", "id": "m",
                                   "content": [{"type": "input_text", "text": text}]})
        self.item(turn, {"type": "UserMessage", "id": f"u-{turn}",
                         "content": [{"type": "text", "text": text, "text_elements": []}]})
        return self

    def reasoning(self, turn: str, summary: list[str] | None = None) -> "Rollout":
        self.item(turn, {"type": "Reasoning", "id": f"r-{turn}-{self.ordinal}",
                         "summary_text": summary or [], "raw_content": []})
        self.add("response_item", {"type": "reasoning", "id": "rs", "summary": [],
                                   "encrypted_content": "gAAAAENCRYPTED"})
        return self

    def reply(self, turn: str, text: str, phase: str = "final_answer") -> "Rollout":
        self.item(turn, {"type": "AgentMessage", "id": f"a-{turn}-{self.ordinal}", "phase": phase,
                         "content": [{"type": "text", "text": text}]})
        self.add("response_item", {"type": "message", "role": "assistant", "id": "m", "phase": phase,
                                   "content": [{"type": "output_text", "text": text}]})
        return self

    def tool(self, turn: str, command: str, output: str = "ok", exit_code: int = 0,
             with_result: bool = True) -> str:
        self._call += 1
        call_id = f"call_{self._call}"
        self.add("response_item", {"type": "custom_tool_call", "id": "ct", "call_id": call_id,
                                   "name": "exec", "status": "completed", "input": f"run({command!r})"})
        self.item(turn, {"type": "CommandExecution", "id": f"item_{self._call}", "command": command,
                         "cwd": "D:\\ctr-testbed", "process_id": "1", "source": "agent",
                         "status": "completed" if exit_code == 0 else "failed", "exit_code": exit_code,
                         "aggregated_output": output, "duration": {"secs": 0, "nanos": 5_000_000},
                         "parsed_cmd": []})
        if with_result:
            self.add("response_item", {"type": "custom_tool_call_output", "id": "co", "call_id": call_id,
                                       "output": f"Exit code: {exit_code}\n{output}"})
        return call_id

    def tokens(self, turn: str, amount: int = 1000) -> "Rollout":
        self.thread_tokens += amount
        self.add("token_usage_record", {"session_id": self.sid, "thread_id": self.sid, "turn_id": turn,
                                        "root_turn_id": turn, "response_id": f"resp_{self.ordinal}",
                                        "usage": usage(amount), "turn_token_usage": usage(amount),
                                        "thread_token_usage": usage(self.thread_tokens)})
        self.add("event_msg", {"type": "token_count", "rate_limits": {"limit_id": "codex"},
                               "info": {"last_token_usage": usage(amount),
                                        "total_token_usage": usage(self.thread_tokens),
                                        "model_context_window": 272000}})
        return self

    def complete(self, turn: str, error: dict | None = None) -> "Rollout":
        p = {"type": "task_complete", "turn_id": turn, "root_turn_id": turn, "started_at": 1000,
             "completed_at": 2000, "duration_ms": 1000, "time_to_first_token_ms": 300,
             "last_agent_message": "done"}
        if error is not None:
            p["error"] = error
        self.add("event_msg", p)
        return self

    def abort(self, turn: str) -> "Rollout":
        self.add("event_msg", {"type": "turn_aborted", "turn_id": turn, "root_turn_id": turn,
                               "reason": "interrupted", "started_at": 1000, "completed_at": 1500,
                               "duration_ms": 500})
        return self

    def simple_turn(self, turn: str, prompt: str, reply: str, model: str = "gpt-6-luna") -> "Rollout":
        return self.start(turn, model).prompt(turn, prompt).reasoning(turn).reply(turn, reply) \
            .tokens(turn).complete(turn)

    def data(self) -> bytes:
        return b"".join(line + b"\n" for line in self.lines())

    def lines(self) -> list[bytes]:
        return [json.dumps(r, ensure_ascii=False).encode("utf-8") for r in self.records]


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="csr-test-"))
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir()
        self.db_path = self.tmp / "out" / "csr.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, data: bytes, sid: str = SID) -> Path:
        path = self.sessions / file_name(sid)
        path.write_bytes(data)
        return path

    def append(self, data: bytes, sid: str = SID) -> Path:
        path = self.sessions / file_name(sid)
        with open(path, "ab") as f:
            f.write(data)
        return path
