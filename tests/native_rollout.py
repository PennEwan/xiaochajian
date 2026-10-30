"""Rollout lines shaped like Codex Desktop 26.1013.4894.0 / kernel 0.163.0-alpha.10.2, for the native plugin tests.

Each user-visible thing appears twice in a real rollout (event_msg/item_completed and response_item),
and token usage twice (token_usage_record and event_msg/token_count) — exactly the duplication the
collector must not count twice.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

VERSION = "0.163.0-alpha.10.2"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def name_for(sid: str) -> str:
    return f"rollout-2026-10-29T00-00-00-{sid}.jsonl"


class Rollout:
    def __init__(self, sid: str, start: datetime, cwd: str = "D:\\work\\proj", step_ms: int = 100):
        self.sid, self.clock, self.cwd, self.step = sid, start, cwd, timedelta(milliseconds=step_ms)
        self.ordinal = 0
        self.lines: list[str] = []
        self.calls = 0

    def at(self, when: datetime) -> "Rollout":
        self.clock = when
        return self

    def add(self, rtype: str, payload: dict) -> "Rollout":
        rec = {"timestamp": iso(self.clock), "ordinal": self.ordinal, "type": rtype, "payload": payload}
        self.lines.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.ordinal += 1
        self.clock += self.step
        return self

    def meta(self) -> "Rollout":
        return self.add("session_meta", {"id": self.sid, "session_id": self.sid, "cwd": self.cwd, "originator": "Codex Desktop",
                                         "cli_version": VERSION, "base_instructions": "SYSTEM PROMPT TEXT",
                                         "creator_user_id": "user-SECRET", "creator_account_id": "acct-SECRET"})

    def item(self, turn: str, it: dict) -> "Rollout":
        return self.add("event_msg", {"type": "item_completed", "thread_id": self.sid, "turn_id": turn,
                                      "started_at_ms": 1, "completed_at_ms": 2, "item": it})

    def start(self, turn: str, model: str = "gpt-6-luna") -> "Rollout":
        self.add("event_msg", {"type": "task_started", "turn_id": turn, "started_at": 1000,
                               "turn_attribution": {"turn_trigger": "composer"}})
        self.add("world_state", {"full": True, "state": {"base_instructions": "SYSTEM"}})
        return self.add("turn_context", {"turn_id": turn, "model": model, "effort": "xhigh", "summary": "none",
                                         "approval_policy": "on-request", "cwd": self.cwd,
                                         "sandbox_policy": {"type": "workspace-write"}})

    def prompt(self, turn: str, text: str) -> "Rollout":
        self.add("response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "DEV INSTRUCTIONS"}]})
        self.add("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})
        return self.item(turn, {"type": "UserMessage", "id": f"u-{turn}", "content": [{"type": "text", "text": text}]})

    def reasoning(self, turn: str, summary: str | None = None) -> "Rollout":
        self.item(turn, {"type": "Reasoning", "id": f"r-{turn}-{self.ordinal}", "summary_text": [summary] if summary else [],
                         "raw_content": []})
        return self.add("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "gAAAAENCRYPTED"})

    def reply(self, turn: str, text: str) -> "Rollout":
        self.item(turn, {"type": "AgentMessage", "id": f"a-{turn}-{self.ordinal}", "phase": "final_answer",
                         "content": [{"type": "text", "text": text}]})
        return self.add("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})

    def tool(self, turn: str, command: str, output: str = "ok") -> "Rollout":
        self.calls += 1
        cid = f"call_{self.calls}"
        self.add("response_item", {"type": "custom_tool_call", "call_id": cid, "name": "exec", "status": "completed",
                                   "input": f"run({command!r})"})
        self.item(turn, {"type": "CommandExecution", "id": f"item_{self.calls}", "command": command, "status": "completed",
                         "exit_code": 0, "aggregated_output": output, "duration": {"secs": 0, "nanos": 5000000}})
        return self.add("response_item", {"type": "custom_tool_call_output", "call_id": cid, "output": f"Exit code: 0\n{output}"})

    def tokens(self, turn: str) -> "Rollout":
        u = {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}
        self.add("token_usage_record", {"turn_id": turn, "response_id": f"resp_{self.ordinal}", "usage": u,
                                        "turn_token_usage": u, "thread_token_usage": u})
        return self.add("event_msg", {"type": "token_count", "rate_limits": {"primary": {"used_percent": 3}}, "info": {}})

    def complete(self, turn: str) -> "Rollout":
        return self.add("event_msg", {"type": "task_complete", "turn_id": turn, "started_at": 1000, "completed_at": 2000,
                                      "duration_ms": 1000, "time_to_first_token_ms": 300, "last_agent_message": "done"})

    def turn(self, turn: str, prompt: str, reply: str, command: str | None = None) -> "Rollout":
        self.start(turn).prompt(turn, prompt).reasoning(turn, "thinking summary")
        if command:
            self.tool(turn, command)
        return self.reply(turn, reply).tokens(turn).complete(turn)

    def old_format_turn(self, prompt: str, reply: str) -> "Rollout":
        """Records older Codex versions wrote instead of item_completed (no local sample to map them safely)."""
        self.add("event_msg", {"type": "user_message", "message": prompt})
        return self.add("event_msg", {"type": "agent_message", "message": reply})

    def text(self) -> str:
        return "".join(self.lines)


# One full turn yields exactly these events — each model output and tool call once.
TURN_KINDS = {"turn_started": 1, "model_config": 1, "user_prompt": 1, "reasoning_summary": 1, "assistant_message": 1,
              "token_usage": 1, "turn_completed": 1}
TOOL_KINDS = {"tool_call": 1, "tool_execution": 1, "tool_result": 1}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
