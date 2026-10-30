import json
import unittest
from collections import Counter

from csr import parser

from csr_helpers import SID, VERSION, Rollout


def parse_all(lines, ctx=None):
    ctx = ctx or parser.Context(session_id=SID)
    events, issues, skipped, offset = [], [], Counter(), 0
    for raw in lines:
        out = parser.parse_line(raw, offset, ctx, "rollout-test.jsonl")
        events += out.events
        issues += out.issues
        skipped.update(out.skipped)
        offset += len(raw) + 1
    return events, issues, skipped, ctx


def kinds(events):
    return [e["kind"] for e in events]


def one(events, kind):
    found = [e for e in events if e["kind"] == kind]
    assert len(found) == 1, (kind, kinds(events))
    return found[0]


class SessionMetaTest(unittest.TestCase):
    def test_maps_config_and_drops_account_identifiers(self):
        events, _, skipped, ctx = parse_all(Rollout().meta().lines())
        e = one(events, "session_meta")
        self.assertEqual(e["event_key"], f"{SID}:0:0")
        self.assertEqual(e["schema"], "csr.event/v1")
        self.assertEqual(e["data"]["cli_version"], VERSION)
        self.assertEqual(e["data"]["originator"], "Codex Desktop")
        self.assertEqual(e["data"]["base_instructions_length"], len("SYSTEM PROMPT TEXT"))
        dumped = json.dumps(events)
        for secret in ("SECRETUSER", "SECRETACCOUNT", "SYSTEM PROMPT TEXT"):
            self.assertNotIn(secret, dumped)
        self.assertEqual(skipped["omitted:session_meta.creator_ids"], 1)
        self.assertEqual(ctx.codex_version, VERSION)
        self.assertEqual(e["source"]["codex_version"], VERSION)

    def test_session_id_from_meta_wins_and_mismatch_is_reported(self):
        ctx = parser.Context(session_id="from-file-name")
        events, issues, _, ctx = parse_all(Rollout().meta().lines(), ctx)
        self.assertEqual(ctx.session_id, SID)
        self.assertEqual(events[0]["session_id"], SID)
        self.assertEqual([i.code for i in issues], ["session_id_mismatch"])


class TurnTest(unittest.TestCase):
    def setUp(self):
        r = Rollout().meta()
        r.start("t1").prompt("t1", "第一条任务").reasoning("t1", ["先看看目录"]) \
            .reply("t1", "进度说明", phase="commentary")
        self.call_id = r.tool("t1", "dir", output="a.txt")
        r.reply("t1", "完成").tokens("t1", 1200).complete("t1")
        self.events, self.issues, self.skipped, self.ctx = parse_all(r.lines())

    def test_prompt_reply_and_model_belong_to_the_turn(self):
        p = one(self.events, "user_prompt")
        self.assertEqual(p["data"]["text"], "第一条任务")
        self.assertEqual((p["turn_id"], p["model"], p["item_id"]), ("t1", "gpt-6-luna", "u-t1"))
        replies = [e for e in self.events if e["kind"] == "assistant_message"]
        self.assertEqual([(e["data"]["text"], e["data"]["phase"]) for e in replies],
                         [("进度说明", "commentary"), ("完成", "final_answer")])
        self.assertEqual(self.issues, [])

    def test_response_item_duplicates_and_policy_records_are_only_counted(self):
        self.assertEqual(self.skipped["duplicate:response_item/message"], 3)
        self.assertEqual(self.skipped["duplicate:response_item/reasoning"], 1)
        self.assertEqual(self.skipped["omitted:encrypted_reasoning"], 1)
        self.assertEqual(self.skipped["duplicate:event_msg/token_count"], 1)
        self.assertEqual(self.skipped["omitted:rate_limits"], 1)
        self.assertEqual(self.skipped["omitted:world_state"], 1)
        self.assertNotIn("ENCRYPTED", json.dumps(self.events))

    def test_turn_config_and_lifecycle(self):
        cfg = one(self.events, "model_config")
        self.assertEqual(cfg["data"]["scope"], "turn")
        self.assertEqual(cfg["data"]["reasoning_effort"], "xhigh")
        self.assertEqual(cfg["data"]["sandbox_policy"], "workspace-write")
        self.assertEqual(one(self.events, "turn_started")["turn_id"], "t1")
        done = one(self.events, "turn_completed")
        self.assertEqual((done["data"]["status"], done["data"]["duration_ms"]), ("completed", 1000))

    def test_reasoning_summary(self):
        r = one(self.events, "reasoning_summary")
        self.assertEqual(r["data"]["summary"], "先看看目录")
        self.assertEqual(r["missing"], [])

    def test_tool_call_result_and_execution(self):
        call = one(self.events, "tool_call")
        self.assertEqual((call["call_id"], call["data"]["tool_name"]), (self.call_id, "exec"))
        self.assertEqual((call["turn_id"], call["turn_id_inferred"]), ("t1", True))
        result = one(self.events, "tool_result")
        self.assertEqual((result["call_id"], result["data"]["outcome"]), (self.call_id, "success"))
        ex = one(self.events, "tool_execution")
        self.assertEqual((ex["data"]["tool_name"], ex["data"]["exit_code"], ex["data"]["output"]),
                         ("CommandExecution", 0, "a.txt"))
        self.assertEqual((ex["turn_id"], ex["turn_id_inferred"], ex["item_id"]), ("t1", False, "item_1"))

    def test_token_usage_from_usage_record(self):
        t = one(self.events, "token_usage")
        self.assertEqual(t["turn_id"], "t1")
        self.assertEqual(t["data"]["thread_token_usage"]["total_tokens"], 1200)


class EdgeTurnTest(unittest.TestCase):
    def test_empty_reasoning_summary_is_marked_unavailable(self):
        r = Rollout().meta().start("t1").reasoning("t1")
        e = one(parse_all(r.lines())[0], "reasoning_summary")
        self.assertIsNone(e["data"]["summary"])
        self.assertEqual([m["field"] for m in e["missing"]], ["summary"])
        self.assertIn("未返回", e["missing"][0]["reason"])

    def test_model_switch_between_turns(self):
        r = Rollout().meta()
        r.simple_turn("t1", "a", "b", model="gpt-6-luna")
        r.thread_settings("gpt-6.1-sol")
        r.simple_turn("t2", "c", "d", model="gpt-6.1-sol")
        events = parse_all(r.lines())[0]
        changed = one(events, "model_changed")
        self.assertEqual((changed["data"]["from"], changed["data"]["to"], changed["turn_id"]),
                         ("gpt-6-luna", "gpt-6.1-sol", "t2"))
        started2 = [e for e in events if e["kind"] == "turn_started"][1]
        self.assertEqual(started2["model"], "gpt-6.1-sol")  # from the thread setting, before turn_context
        prompts = [(e["turn_id"], e["model"]) for e in events if e["kind"] == "user_prompt"]
        self.assertEqual(prompts, [("t1", "gpt-6-luna"), ("t2", "gpt-6.1-sol")])
        self.assertEqual(sum(1 for e in events if e["kind"] == "model_config"), 3)

    def test_turn_error_and_abort(self):
        r = Rollout().meta()
        r.start("t1").prompt("t1", "x").complete("t1", error={"message": "busy",
                                                              "codex_error_info": "server_overloaded"})
        r.start("t2").prompt("t2", "y").abort("t2")
        events = parse_all(r.lines())[0]
        self.assertEqual(one(events, "turn_completed")["data"]["status"], "error")
        err = one(events, "turn_error")
        self.assertEqual((err["turn_id"], err["data"]["codex_error_info"]), ("t1", "server_overloaded"))
        aborted = one(events, "turn_aborted")
        self.assertEqual((aborted["turn_id"], aborted["data"]["reason"]), ("t2", "interrupted"))

    def test_tool_records_after_turn_end_are_not_attributed_to_it(self):
        r = Rollout().meta().simple_turn("t1", "a", "b")
        r.add("response_item", {"type": "custom_tool_call", "call_id": "late", "name": "exec", "input": ""})
        call = one(parse_all(r.lines())[0], "tool_call")
        self.assertIsNone(call["turn_id"])
        self.assertIn("turn_id", [m["field"] for m in call["missing"]])


class RedactionTest(unittest.TestCase):
    def test_secrets_in_prompt_are_redacted(self):
        r = Rollout().meta().start("t1").prompt("t1", "key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX1234 please")
        e = one(parse_all(r.lines())[0], "user_prompt")
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWX1234", e["data"]["text"])
        self.assertEqual(e["redactions"], {"openai_key": 1})

    def test_output_of_tools_touching_credential_files_is_dropped(self):
        r = Rollout().meta().start("t1")
        r.tool("t1", "type C:\\Users\\me\\.codex\\auth.json", output='{"tokens": "SECRET-OUTPUT"}')
        events = parse_all(r.lines())[0]
        dumped = json.dumps(events)
        self.assertNotIn("SECRET-OUTPUT", dumped)
        self.assertEqual(one(events, "tool_result")["data"]["output"], "[REDACTED:sensitive_file]")
        self.assertEqual(one(events, "tool_execution")["data"]["output"], "[REDACTED:sensitive_file]")


class BadInputTest(unittest.TestCase):
    def test_parse_failures_are_issues_not_exceptions(self):
        lines = [b"{not json", b"[1,2]", b'{"payload": {}}', b'{"type": "event_msg", "payload": 3}',
                 b"\xff\xfe{}"]
        events, issues, _, _ = parse_all(lines)
        self.assertEqual(events, [])
        self.assertEqual([i.code for i in issues],
                         ["bad_json", "not_object", "missing_type", "bad_payload", "bad_encoding"])
        self.assertTrue(all(i.category == "parse" for i in issues))
        self.assertNotIn("not json", " ".join(i.detail for i in issues))

    def test_ordinal_gap_is_reported_once_and_bad_lines_are_not_double_counted(self):
        lines = Rollout().meta().simple_turn("t1", "a", "b").lines()
        lines[3] = b"{broken"
        del lines[6:8]
        _, issues, _, _ = parse_all(lines)
        self.assertEqual([(i.code, i.count) for i in issues], [("bad_json", 1), ("ordinal_gap", 2)])
        self.assertEqual(issues[1].category, "missing")

    def test_unknown_record_types_are_counted_by_name(self):
        r = Rollout().meta()
        r.add("brand_new", {"x": 1})
        r.add("event_msg", {"type": "shiny_event"})
        r.item("t1", {"type": "FutureItem", "id": "f"})
        _, issues, skipped, _ = parse_all(r.lines())
        self.assertEqual(skipped["unrecognized:brand_new"], 1)
        self.assertEqual(skipped["unrecognized:event_msg/shiny_event"], 1)
        self.assertEqual(skipped["unrecognized:event_msg/item_completed:FutureItem"], 1)
        self.assertEqual(issues, [])

    def test_missing_timestamp_is_kept_with_reason(self):
        r = Rollout().meta().start("t1").prompt("t1", "x")
        del r.records[-1]["timestamp"]
        e = one(parse_all(r.lines())[0], "user_prompt")
        self.assertIsNone(e["ts"])
        self.assertIn("ts", [m["field"] for m in e["missing"]])

    def test_records_without_ordinal_use_the_byte_offset_as_key(self):
        r = Rollout().meta()
        del r.records[0]["ordinal"]
        e = parse_all(r.lines())[0][0]
        self.assertEqual(e["event_key"], f"{SID}:@0:0")
        self.assertIsNone(e["ordinal"])


class ContextTest(unittest.TestCase):
    def test_round_trip_keeps_resume_state(self):
        r = Rollout().meta().start("t1", model="gpt-x")
        _, _, _, ctx = parse_all(r.lines())
        restored = parser.Context.from_json(ctx.to_json())
        self.assertEqual(restored, ctx)
        r2 = Rollout()
        r2.ordinal = r.ordinal
        r2.prompt("t1", "later")
        e = one(parse_all(r2.lines(), restored)[0], "user_prompt")
        self.assertEqual((e["model"], e["turn_id"]), ("gpt-x", "t1"))


if __name__ == "__main__":
    unittest.main()
