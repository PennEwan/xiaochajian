"""P3 acceptance criteria, end to end on synthetic hook payloads and transcripts:
hooks -> session files -> uploader -> mock backend (scripts/mock_sync_server.py)."""
import unittest
from collections import Counter

from ctr.sync.worker import Worker

from helpers import rec, write_rollout
from sync_helpers import MockSync, SyncCase
from test_record_v2 import turn_records

TURN_KINDS = {"user_prompt", "assistant_message", "tool_call", "tool_result", "turn_start", "turn_end",
              "token_usage", "model_config", "reasoning_summary", "turn_coverage"}


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class AcceptanceCase(SyncCase):
    def setUp(self):
        super().setUp()
        self.mock = MockSync()
        self.addCleanup(self.mock.stop)
        self.sync_on(at=-60, base_url=self.mock.url)
        self.time = FakeTime()

    def upload(self, **kw):
        kw.setdefault("idle_exit_s", 5)
        w = Worker(self.paths, clock=self.time.clock, sleep=self.time.sleep, rng=lambda: 0.5, **kw)
        return w.run()

    def codex_turn(self, turn, model, t0, **records):
        """What Codex does for one turn with one shell command: hooks + transcript lines."""
        with open(self.rollout, "a", encoding="utf-8", newline="\n") as f:
            import json
            for r in turn_records(t0, turn, model, **records):
                f.write(json.dumps(r) + "\n")
        self.hook("UserPromptSubmit", turn_id=turn, model=model, prompt=f"prompt {turn}")
        if records.get("tools", True):
            self.hook("PreToolUse", turn_id=turn, model=model, tool_name="Bash", tool_use_id=f"exec-{turn}",
                      tool_input={"command": "dir"})
            self.hook("PostToolUse", turn_id=turn, model=model, tool_name="Bash", tool_use_id=f"exec-{turn}",
                      tool_input={"command": "dir"}, tool_response="a.txt")
        self.hook("Stop", turn_id=turn, model=model, last_assistant_message="done", stop_hook_active=False)

    def remote(self, **match):
        return [e for e in self.mock.events() if all(e.get(k) == v for k, v in match.items())]


class Acceptance(AcceptanceCase):
    def test_1_a_codex_turn_reaches_the_backend(self):
        self.codex_turn("t1", "gpt-a", 1)
        self.assertEqual(self.upload(), "idle")
        kinds = {e["kind"] for e in self.remote(turn_id="t1")}
        self.assertTrue(TURN_KINDS <= kinds, TURN_KINDS - kinds)
        self.assertEqual(sorted(e["event_id"] for e in self.mock.events()),
                         sorted(e["event_id"] for e in self.events()))

    def test_2_sync_needs_no_recharge_window_or_plugin(self):
        # Nothing from codex-cdk-recharge exists in this data directory or process; the
        # recorder's own hooks and uploader carry everything (see IndependenceTest too).
        self.codex_turn("t1", "gpt-a", 1)
        self.assertEqual(self.upload(), "idle")
        self.assertTrue(self.remote(turn_id="t1"))
        created = {p.name for p in self.paths.root.iterdir()}
        self.assertFalse(created & {"binding.bin", "CodexCdkRecharge"}, created)

    def test_3_model_switch_and_tool_calls_are_linked_to_their_turn(self):
        self.codex_turn("t1", "gpt-a", 1)
        self.codex_turn("t2", "gpt-b", 20)
        self.upload()
        for turn, model in (("t1", "gpt-a"), ("t2", "gpt-b")):
            transcript_models = {e["model"] for e in self.remote(turn_id=turn) if e["source"]["type"] == "transcript"}
            self.assertEqual(transcript_models, {model}, turn)
            hook_tools = [e for e in self.remote(turn_id=turn, kind="tool_call") if e["source"]["type"] == "hook"]
            self.assertEqual([e["tool_use_id"] for e in hook_tools], [f"exec-{turn}"])
            self.assertEqual(hook_tools[0]["model"], model)
            results = self.remote(turn_id=turn, kind="tool_result")
            self.assertIn(f"exec-{turn}", {e["tool_use_id"] for e in results} | {e["item_id"] for e in results})
            calls = {e["tool_use_id"] for e in self.remote(turn_id=turn, kind="tool_call")
                     if e["granularity"] == "model_tool_call"}
            outputs = {e["tool_use_id"] for e in results if e["granularity"] == "model_tool_call"}
            self.assertEqual(calls, outputs, "model-level call and output share the call id")
        changed = {(e["data"]["from"], e["data"]["to"]) for e in self.remote(kind="model_changed")}
        self.assertIn(("gpt-a", "gpt-b"), changed)

    def test_4_after_sync_is_switched_off_nothing_new_is_collected(self):
        self.codex_turn("t1", "gpt-a", 1)
        self.upload()
        before = len(self.mock.events())
        self.sync_pause(at=10**6)
        self.codex_turn("t2", "gpt-b", 20)
        self.assertEqual(self.upload(), "disabled")
        self.assertEqual(len(self.mock.events()), before)
        self.assertEqual(self.remote(turn_id="t2"), [])
        self.assertTrue([e for e in self.events() if e["turn_id"] == "t2"], "local recording continues")

    def test_5_network_failures_recover_without_duplicates(self):
        self.codex_turn("t1", "gpt-a", 1)
        self.mock.fail_next(status=503, count=2)
        self.mock.fail_next(mode="drop_after_store")
        self.assertEqual(self.upload(), "idle")
        self.codex_turn("t2", "gpt-a", 20)
        self.mock.fail_next(mode="drop_after_store")
        self.assertEqual(self.upload(), "idle")
        ids = [e["event_id"] for e in self.mock.events()]
        self.assertEqual(Counter(ids).most_common(1)[0][1], 1, "no event stored twice")
        self.assertEqual(sorted(ids), sorted(e["event_id"] for e in self.events()))
        by_arrival = sorted(self.mock.events(), key=lambda e: e["_arrival"])
        seqs = [e["seq"] for e in by_arrival]
        self.assertEqual(seqs, sorted(seqs), "the backend received each event after its predecessors")

    def test_6_missing_and_unsupported_content_is_marked(self):
        self.codex_turn("t1", "gpt-a", 1, tools=False, tokens=False, summary=False)
        self.hook("UserPromptSubmit", sid="bg-1", transcript=False, prompt="# Overview")
        self.hook("Stop", sid="bg-1", transcript=False, last_assistant_message="[]", stop_hook_active=False)
        self.upload()
        [cov] = self.remote(kind="turn_coverage", session_id=self.events()[0]["session_id"])
        self.assertEqual(cov["data"]["missing"], ["reasoning_summary", "token_usage"])
        self.assertEqual(cov["data"]["items"]["reasoning_summary"]["status"], "unavailable")
        [bg] = self.remote(kind="turn_coverage", session_id="bg-1")
        self.assertEqual(bg["session_kind"], "background")
        self.assertIn("token_usage", bg["data"]["missing"])
        reasoning = self.remote(kind="reasoning_summary", turn_id="t1")
        self.assertEqual(reasoning[0]["completeness"]["status"], "unavailable")
        caps = self.mock.state.envelopes[0]["capabilities"]
        self.assertIn("hook_source_timestamps", caps["not_provided_by_codex"])
        self.assertIn("encrypted_reasoning", caps["not_collected_by_policy"])


class PrivacyTest(AcceptanceCase):
    def test_secrets_cdks_and_diagnostics_never_reach_the_backend(self):
        cdk, key = "AB12-CD34-EF56-GH78", "sk-proj-" + "Z" * 30
        self.hook("UserPromptSubmit", prompt=f"兑换码：{cdk}，key {key}")
        write_rollout(self.rollout, [rec(5, "event_msg", {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "UserMessage", "id": "u", "content": [{"type": "text", "text": f"cdk={cdk}"}]}})])
        self.hook("UserPromptSubmit", prompt="/充值 " + cdk, turn_id="t2")
        self.upload()
        import json
        blob = json.dumps(self.mock.events(), ensure_ascii=False)
        for secret in (cdk, key, "PLUGIN_ROOT", "process_chain", "python_executable"):
            self.assertNotIn(secret, blob, secret)


if __name__ == "__main__":
    unittest.main()
