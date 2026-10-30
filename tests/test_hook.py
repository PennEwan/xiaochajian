import unittest
from datetime import timedelta

from ctr import hook, state

from helpers import T0, TempPathsCase, rec, write_rollout

SID = "01a1634e-e806-7252-8f31-c4c0d9bf63c2"
DEFAULT_TRANSCRIPT = "<default-transcript>"  # replaced by a real empty file in RecordingTest


def payload(event, **kw):
    base = {"hook_event_name": event, "session_id": SID, "cwd": r"D:\ctr-testbed",
            "model": "gpt-test", "permission_mode": "default", "transcript_path": DEFAULT_TRANSCRIPT}
    if event not in ("SessionStart", "SessionEnd"):
        base["turn_id"] = "turn-1"
    base.update(kw)
    return base


class NotConsentedTest(TempPathsCase):
    def test_prompt_gets_one_not_enabled_notice_and_nothing_is_recorded(self):
        out = self.run_hook(payload("UserPromptSubmit", prompt="hello"))
        self.assertEqual(out, {"systemMessage": hook.NOTICE_NOT_ENABLED})
        self.assertIsNone(self.run_hook(payload("UserPromptSubmit", prompt="again")))
        self.assertFalse(self.paths.sessions.exists())
        self.assertFalse(self.paths.cursors.exists())

    def test_other_events_write_nothing(self):
        self.assertIsNone(self.run_hook(payload("PreToolUse", tool_name="exec", tool_input={})))
        self.assertFalse(self.paths.root.exists())

    def test_explicitly_disabled_is_silent(self):
        self.enable(at=0)
        state.disable(self.paths.state, T0 + timedelta(seconds=1))
        self.assertIsNone(self.run_hook(payload("UserPromptSubmit", prompt="hi")))
        self.assertFalse(self.paths.sessions.exists())

    def test_kill_switch_env(self):
        self.enable(at=0)
        self.assertIsNone(self.run_hook(payload("UserPromptSubmit", prompt="hi"), env={"CTR_DISABLE": "1"}))
        self.assertFalse(self.paths.sessions.exists())

    def test_out_of_scope_cwd_is_not_recorded(self):
        self.enable(at=0, scope_cwds=[r"D:\elsewhere"])
        self.assertIsNone(self.run_hook(payload("UserPromptSubmit", prompt="hi")))
        self.assertFalse(self.paths.sessions.exists())


class RecordingTest(TempPathsCase):
    def setUp(self):
        super().setUp()
        self.enable(at=-3600)
        self.default_rollout = self.tmp / "default-rollout.jsonl"
        write_rollout(self.default_rollout, [])

    def run_hook(self, p, env=None):
        if isinstance(p, dict) and p.get("transcript_path") == DEFAULT_TRANSCRIPT:
            p = {**p, "transcript_path": str(self.default_rollout)}
        return super().run_hook(p, env)

    def test_prompt_recorded_with_notice_once(self):
        out = self.run_hook(payload("UserPromptSubmit", prompt="hello"))
        self.assertEqual(out, {"systemMessage": hook.NOTICE_RECORDING})
        self.assertIsNone(self.run_hook(payload("UserPromptSubmit", prompt="second")))
        kinds = self.kinds()
        self.assertEqual(kinds.count("user_prompt"), 2)
        self.assertEqual(kinds.count("recorder_notice"), 1)
        self.assertEqual(kinds.count("model_config"), 1)
        self.assertEqual(kinds.count("hook_invocation"), 2)
        prompt = next(e for e in self.events() if e["kind"] == "user_prompt")
        self.assertEqual(prompt["source"], {**prompt["source"], "type": "hook", "name": "UserPromptSubmit"})
        self.assertEqual(prompt["completeness"]["status"], "complete")
        self.assertIsNone(prompt["source_ts"])

    def test_session_start_never_prints(self):
        self.assertIsNone(self.run_hook(payload("SessionStart", source="startup")))
        self.assertIn("session_start", self.kinds())

    def test_secret_in_prompt_is_redacted(self):
        key = "sk-proj-" + "Q" * 30
        self.run_hook(payload("UserPromptSubmit", prompt=f"my key is {key}"))
        prompt = next(e for e in self.events() if e["kind"] == "user_prompt")
        self.assertNotIn(key, prompt["data"]["text"])
        self.assertTrue(prompt["redaction"]["applied"])

    def test_recharge_command_never_reaches_disk(self):
        cdk = "ZQXW-9F3K-PL7M-2B8N"
        self.run_hook(payload("UserPromptSubmit", prompt=f"/充值 {cdk}"))
        prompt = next(e for e in self.events() if e["kind"] == "user_prompt")
        self.assertEqual(prompt["data"]["text"], "[REDACTED:recharge_command]")
        written = "".join(p.read_text(encoding="utf-8") for p in self.paths.root.rglob("*") if p.is_file())
        self.assertNotIn(cdk, written)

    def test_post_tool_use_on_credential_file_drops_output(self):
        self.run_hook(payload("PostToolUse", tool_name="Bash", tool_use_id="call_1",
                              tool_input={"command": "type .env"},
                              tool_response={"exit_code": 0, "stdout": "DB_PASS=hunter22"}))
        result = next(e for e in self.events() if e["kind"] == "tool_result")
        self.assertEqual(result["data"]["tool_response"], "[REDACTED:sensitive_file]")
        self.assertEqual(result["data"]["outcome"], "success")
        self.assertEqual(result["tool_use_id"], "call_1")

    def test_failed_tool_marked_error(self):
        self.run_hook(payload("PostToolUse", tool_name="Bash", tool_use_id="call_2",
                              tool_input={"command": "false"}, tool_response="Exit code: 1\nboom"))
        result = next(e for e in self.events() if e["kind"] == "tool_result")
        self.assertEqual(result["data"]["outcome"], "error")

    def test_missing_documented_field_marks_partial(self):
        self.run_hook(payload("PreToolUse", tool_name="exec"))
        call = next(e for e in self.events() if e["kind"] == "tool_call")
        self.assertEqual(call["completeness"]["status"], "partial")
        self.assertIn("tool_input", call["completeness"]["missing"])

    def test_undocumented_fields_are_kept_for_discovery(self):
        self.run_hook(payload("Stop", last_assistant_message="done", stop_hook_active=False,
                              brand_new_field=1))
        end = next(e for e in self.events() if e["kind"] == "turn_end")
        self.assertEqual(end["data"]["_undocumented_fields"], {"brand_new_field": 1})

    def test_stop_message_is_partial(self):
        self.run_hook(payload("Stop", last_assistant_message="done", stop_hook_active=False))
        msg = next(e for e in self.events() if e["kind"] == "assistant_message")
        self.assertEqual(msg["completeness"]["status"], "partial")
        self.assertIn("intermediate_messages", msg["completeness"]["missing"])

    def test_model_change_between_hooks(self):
        self.run_hook(payload("UserPromptSubmit", prompt="a", model="gpt-a"))
        self.run_hook(payload("UserPromptSubmit", prompt="b", model="gpt-b"))
        changed = [e for e in self.events() if e["kind"] == "model_changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["data"], {"from": "gpt-a", "to": "gpt-b"})
        self.assertEqual(changed[0]["source"]["type"], "derived")

    def test_session_without_transcript_is_background_and_gets_no_notice(self):
        out = self.run_hook(payload("UserPromptSubmit", prompt="# Overview\nGenerate suggestions",
                                    transcript_path=None))
        self.assertIsNone(out, "nobody sees a background session; no notice")
        self.run_hook(payload("PreToolUse", tool_name="Bash", tool_input={"command": "dir"}, transcript_path=None))
        evs = self.events()
        self.assertTrue(evs)
        self.assertEqual({e["session_kind"] for e in evs}, {"background"})
        self.assertNotIn("recorder_notice", self.kinds())

    def test_session_with_transcript_is_standard(self):
        rollout = self.tmp / "rollout.jsonl"
        write_rollout(rollout, [])
        self.run_hook(payload("UserPromptSubmit", prompt="hi", transcript_path=str(rollout)))
        self.assertEqual({e["session_kind"] for e in self.events()}, {"standard"})

    def test_flush_picks_up_lines_written_after_the_last_hook(self):
        rollout = self.tmp / "rollout.jsonl"
        write_rollout(rollout, [rec(10, "event_msg", {"type": "task_started", "turn_id": "turn-1"})])
        self.run_hook(payload("Interrupt", transcript_path=str(rollout)))
        with open(rollout, "a", encoding="utf-8") as f:
            import json
            f.write(json.dumps(rec(11, "event_msg", {"type": "turn_aborted", "turn_id": "turn-1",
                                                     "reason": "interrupted"})) + "\n")
        st = state.load(self.paths.state)
        self.assertEqual(hook.flush_transcripts(self.paths, st), 2, "turn_aborted + its turn_coverage")
        aborted = [e for e in self.events() if e["source"]["name"] == "event_msg/turn_aborted"]
        self.assertEqual(len(aborted), 1)
        self.assertEqual(self.kinds().count("turn_coverage"), 1)
        self.assertEqual(hook.flush_transcripts(self.paths, st), 0, "second flush adds nothing")

    def test_garbage_stdin_exits_zero_and_logs(self):
        self.assertIsNone(self.run_hook(b"\xff not json"))
        self.assertTrue(self.paths.log.exists())

    def test_stop_ingests_transcript_inside_window_only(self):
        rollout = self.tmp / "rollout.jsonl"
        write_rollout(rollout, [
            rec(-7200, "event_msg", {"type": "item_completed", "turn_id": "t0",
                                     "item": {"type": "AgentMessage", "id": "m0",
                                              "content": [{"type": "text", "text": "before consent"}]}}),
            rec(10, "event_msg", {"type": "item_completed", "turn_id": "turn-1",
                                  "item": {"type": "AgentMessage", "id": "m1",
                                           "content": [{"type": "text", "text": "inside"}]}}),
        ])
        self.run_hook(payload("Stop", transcript_path=str(rollout), last_assistant_message="inside",
                              stop_hook_active=False))
        texts = [e["data"]["text"] for e in self.events()
                 if e["kind"] == "assistant_message" and e["source"]["type"] == "transcript"]
        self.assertEqual(texts, ["inside"])
        summary = next(e for e in self.events() if e["kind"] == "transcript_other")
        self.assertEqual(summary["data"]["counts"]["skipped_outside_consent_window"], 1)
        # A second Stop must not re-ingest the same lines.
        self.run_hook(payload("Stop", transcript_path=str(rollout), last_assistant_message="inside",
                              stop_hook_active=False))
        again = [e for e in self.events()
                 if e["kind"] == "assistant_message" and e["source"]["type"] == "transcript"]
        self.assertEqual(len(again), 1)


if __name__ == "__main__":
    unittest.main()
