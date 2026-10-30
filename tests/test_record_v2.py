"""ctr.event/v2: per-session seq, per-event model, and the derived turn_coverage event."""
import json
import unittest

from ctr import hook

from helpers import TempPathsCase, rec, write_rollout

SID = "01a1634e-e806-7252-8f31-c4c0d9bf63c2"


def item(t, turn, it):
    return rec(t, "event_msg", {"type": "item_completed", "turn_id": turn, "item": it})


def turn_records(t0, turn, model, *, tools=True, tokens=True, summary=True, reasoning=True, end="complete"):
    out = [
        rec(t0, "turn_context", {"turn_id": turn, "model": model, "effort": "low"}),
        rec(t0 + 1, "event_msg", {"type": "task_started", "turn_id": turn}),
        item(t0 + 2, turn, {"type": "UserMessage", "id": f"u-{turn}",
                            "content": [{"type": "text", "text": f"prompt {turn}"}]}),
    ]
    if reasoning:
        out.append(item(t0 + 3, turn, {"type": "Reasoning", "id": f"r-{turn}",
                                       "summary_text": ["thinking"] if summary else []}))
    if tools:
        out += [
            rec(t0 + 4, "response_item", {"type": "custom_tool_call", "name": "exec", "call_id": f"call-{turn}",
                                          "input": "dir"}),
            item(t0 + 5, turn, {"type": "CommandExecution", "id": f"exec-{turn}", "command": "dir",
                                "exit_code": 0, "status": "completed", "aggregated_output": "a.txt"}),
            rec(t0 + 6, "response_item", {"type": "custom_tool_call_output", "call_id": f"call-{turn}",
                                          "output": "Exit code: 0"}),
        ]
    out.append(item(t0 + 7, turn, {"type": "AgentMessage", "id": f"a-{turn}",
                                   "content": [{"type": "text", "text": "done"}]}))
    if tokens:
        out.append(rec(t0 + 8, "event_msg", {"type": "token_count",
                                             "info": {"total_token_usage": {"input_tokens": 9}}}))
    if end == "complete":
        out.append(rec(t0 + 9, "event_msg", {"type": "task_complete", "turn_id": turn, "duration_ms": 9}))
    elif end == "error":
        out.append(rec(t0 + 9, "event_msg", {"type": "task_complete", "turn_id": turn,
                                             "error": {"message": "serverOverloaded"}}))
    elif end == "aborted":
        out.append(rec(t0 + 9, "event_msg", {"type": "turn_aborted", "turn_id": turn,
                                             "reason": "interrupted"}))
    return out


class RecordCase(TempPathsCase):
    def setUp(self):
        super().setUp()
        self.enable(at=-3600)
        self.rollout = self.tmp / "rollout.jsonl"
        write_rollout(self.rollout, [])

    def hook(self, event, transcript=True, **kw):
        p = {"hook_event_name": event, "session_id": SID, "cwd": r"D:\ctr-testbed",
             "model": "gpt-a", "permission_mode": "default",
             "transcript_path": str(self.rollout) if transcript else None}
        if event not in ("SessionStart", "SessionEnd"):
            p["turn_id"] = "t1"
        p.update(kw)
        p = {k: v for k, v in p.items() if v is not None or k == "transcript_path"}
        return self.run_hook(p)

    def of_kind(self, kind):
        return [e for e in self.events() if e["kind"] == kind]


class SeqAndModelTest(RecordCase):
    def test_every_event_is_v2_with_contiguous_seq_in_file_order(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt="hi")
        self.hook("PreToolUse", tool_name="Bash", tool_use_id="exec-1", tool_input={"command": "dir"})
        events = self.events()
        self.assertTrue(events)
        self.assertEqual({e["schema"] for e in events}, {"ctr.event/v2"})
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))

    def test_hook_events_carry_the_hook_model(self):
        self.hook("UserPromptSubmit", prompt="hi")
        self.hook("UserPromptSubmit", prompt="switch", model="gpt-b", turn_id="t2")
        prompts = self.of_kind("user_prompt")
        self.assertEqual([e["model"] for e in prompts], ["gpt-a", "gpt-b"])
        changed = self.of_kind("model_changed")
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["model"], "gpt-b")

    def test_session_end_without_model_uses_last_known_hook_model(self):
        self.hook("UserPromptSubmit", prompt="hi", model="gpt-b")
        self.hook("SessionEnd", reason="exit", model=None)
        self.assertEqual(self.of_kind("session_end")[0]["model"], "gpt-b")

    def test_transcript_events_carry_the_model_of_their_turn(self):
        write_rollout(self.rollout, turn_records(1, "t1", "gpt-a") + turn_records(20, "t2", "gpt-b"))
        self.hook("Stop", turn_id="t2", model="gpt-b", last_assistant_message="done", stop_hook_active=False)
        by_turn = {}
        for e in self.events():
            if e["source"]["type"] == "transcript" and e["turn_id"]:
                by_turn.setdefault(e["turn_id"], set()).add(e["model"])
        self.assertEqual(by_turn, {"t1": {"gpt-a"}, "t2": {"gpt-b"}})
        changed = [e for e in self.of_kind("model_changed") if e["source"]["type"] == "derived"
                   and e["source"]["name"].startswith("turn_context")]
        self.assertEqual([(e["data"]["from"], e["data"]["to"], e["model"]) for e in changed],
                         [("gpt-a", "gpt-b", "gpt-b")])

    def test_seq_continues_across_flush(self):
        self.hook("UserPromptSubmit", prompt="hi")
        before = len(self.events())
        write_rollout(self.rollout, turn_records(1, "t1", "gpt-a"))
        from ctr import state
        hook.flush_transcripts(self.paths, state.load(self.paths.state))
        seqs = [e["seq"] for e in self.events()]
        self.assertGreater(len(seqs), before)
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


class TurnCoverageTest(RecordCase):
    def coverage(self):
        return self.of_kind("turn_coverage")

    def test_completed_turn_reports_each_item(self):
        write_rollout(self.rollout, turn_records(1, "t1", "gpt-a"))
        self.hook("UserPromptSubmit", prompt="prompt t1")
        self.hook("Stop", last_assistant_message="done", stop_hook_active=False)
        [cov] = self.coverage()
        items = cov["data"]["items"]
        self.assertEqual(cov["turn_id"], "t1")
        self.assertEqual(cov["data"]["turn_status"], "completed")
        self.assertEqual(items["user_prompt"]["status"], "captured")
        self.assertEqual(items["assistant_messages"]["status"], "captured")
        self.assertEqual(items["reasoning_summary"]["status"], "captured")
        self.assertEqual(items["model"], {**items["model"], "status": "captured", "values": ["gpt-a"]})
        self.assertEqual(items["tool_calls"]["status"], "captured")
        self.assertEqual(items["token_usage"]["status"], "captured")
        self.assertEqual(items["timestamps"]["status"], "partial")
        self.assertEqual(cov["data"]["missing"], [])
        self.assertEqual(cov["completeness"]["status"], "complete")
        self.assertEqual(cov["model"], "gpt-a")
        self.assertEqual(cov["source"]["type"], "derived")

    def test_missing_items_are_named(self):
        write_rollout(self.rollout, turn_records(1, "t1", "gpt-a", tools=False, tokens=False, summary=False))
        self.hook("Stop", last_assistant_message="done", stop_hook_active=False)
        [cov] = self.coverage()
        items = cov["data"]["items"]
        self.assertEqual(items["reasoning_summary"]["status"], "unavailable")
        self.assertEqual(items["token_usage"]["status"], "not_captured")
        self.assertEqual(items["tool_calls"]["status"], "none")
        self.assertEqual(cov["data"]["missing"], ["reasoning_summary", "token_usage"])
        self.assertEqual(cov["completeness"]["status"], "partial")
        self.assertEqual(cov["completeness"]["missing"], ["reasoning_summary", "token_usage"])

    def test_turn_without_any_reasoning_item(self):
        # Seen live with gpt-5.6-luna at low effort: the transcript has no Reasoning item at all.
        write_rollout(self.rollout, turn_records(1, "t1", "gpt-a", reasoning=False))
        self.hook("Stop", last_assistant_message="done", stop_hook_active=False)
        [cov] = self.coverage()
        item_ = cov["data"]["items"]["reasoning_summary"]
        self.assertEqual(item_["status"], "unavailable")
        self.assertIn("没有推理", item_["note"])

    def test_failed_and_interrupted_turns(self):
        write_rollout(self.rollout, turn_records(1, "t1", "gpt-a", tokens=False, end="error")
                      + turn_records(20, "t2", "gpt-a", tokens=False, end="aborted"))
        self.hook("UserPromptSubmit", prompt="next", turn_id="t3")
        statuses = {c["turn_id"]: c["data"] for c in self.coverage()}
        self.assertEqual(statuses["t1"]["turn_status"], "failed")
        self.assertEqual(statuses["t2"]["turn_status"], "interrupted")
        self.assertEqual(statuses["t1"]["items"]["token_usage"]["status"], "none")

    def test_one_coverage_per_turn_even_if_ingested_twice(self):
        write_rollout(self.rollout, turn_records(1, "t1", "gpt-a"))
        self.hook("Stop", last_assistant_message="done", stop_hook_active=False)
        self.hook("Interrupt")  # Interrupt for the same turn id must not add a second summary
        self.assertEqual(len(self.coverage()), 1)

    def test_background_session_without_transcript(self):
        self.hook("UserPromptSubmit", transcript=False, prompt="# Overview Generate suggestions")
        self.hook("PreToolUse", transcript=False, tool_name="Bash", tool_use_id="exec-1",
                  tool_input={"command": "dir"})
        self.hook("Stop", transcript=False, last_assistant_message="[]", stop_hook_active=False)
        [cov] = self.coverage()
        items = cov["data"]["items"]
        self.assertEqual(cov["session_kind"], "background")
        self.assertEqual(items["assistant_messages"]["status"], "partial")
        self.assertEqual(items["reasoning_summary"]["status"], "unavailable")
        self.assertEqual(items["token_usage"]["status"], "unavailable")
        self.assertIn("transcript", items["token_usage"]["note"])
        self.assertIn("token_usage", cov["data"]["missing"])

    def test_tally_is_cleared_after_coverage(self):
        write_rollout(self.rollout, turn_records(1, "t1", "gpt-a"))
        self.hook("Stop", last_assistant_message="done", stop_hook_active=False)
        cursor = json.loads(next(self.paths.cursors.glob("*.json")).read_text(encoding="utf-8"))
        self.assertNotIn("t1", cursor.get("turn_tally", {}))


if __name__ == "__main__":
    unittest.main()
