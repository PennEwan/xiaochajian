import hashlib
import json
import os
import random
import sqlite3
import unittest
from unittest import mock

from csr import parser, scanner
from csr.storage import Storage

from csr_helpers import SID, SID2, Rollout, TempDirCase, file_name


def three_prompts() -> Rollout:
    r = Rollout().meta()
    r.simple_turn("t1", "任务一", "回复一")
    r.start("t2").prompt("t2", "任务二").reasoning("t2", ["检查目录结构"]).reply("t2", "我先运行命令", "commentary")
    r.tool("t2", "dir", output="a.txt")
    r.tool("t2", "python missing.py", output="No such file", exit_code=1)
    r.reply("t2", "回复二").tokens("t2", 2000).complete("t2")
    r.thread_settings("gpt-6.1-sol")
    r.simple_turn("t3", "任务三", "回复三", model="gpt-6.1-sol")
    return r


class ScanCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.storage.close()
        super().tearDown()

    def scan(self, storage=None, **kw):
        return scanner.scan(storage or self.storage, self.sessions, **kw)

    def keys(self, storage=None):
        return [e["event_key"] for e in (storage or self.storage).iter_events()]

    def bodies(self, storage=None):
        return [json.dumps(e, sort_keys=True) for e in (storage or self.storage).iter_events()]

    def reference(self, data: bytes) -> list[str]:
        """Events from one uninterrupted scan of `data` in a separate directory and database."""
        other = self.tmp / "reference"
        other.mkdir()
        (other / file_name(SID)).write_bytes(data)
        ref = Storage(self.tmp / "ref.sqlite3")
        try:
            scanner.scan(ref, other)
            return [json.dumps(e, sort_keys=True) for e in ref.iter_events()]
        finally:
            ref.close()


class ScenarioTest(ScanCase):
    def test_scenario1_three_prompts_in_order(self):
        self.write(three_prompts().data())
        [res] = self.scan()
        self.assertEqual(res.status, "read")
        prompts = [e for e in self.storage.iter_events() if e["kind"] == "user_prompt"]
        self.assertEqual([(e["turn_id"], e["data"]["text"]) for e in prompts],
                         [("t1", "任务一"), ("t2", "任务二"), ("t3", "任务三")])

    def test_scenario2_replies_and_tool_calls(self):
        self.write(three_prompts().data())
        self.scan()
        by_kind = {}
        for e in self.storage.iter_events():
            by_kind.setdefault(e["kind"], []).append(e)
        self.assertEqual(len(by_kind["assistant_message"]), 4)
        self.assertEqual([e["data"]["outcome"] for e in by_kind["tool_result"]], ["success", "error"])
        self.assertEqual([e["data"]["exit_code"] for e in by_kind["tool_execution"]], [0, 1])

    def test_scenario3_model_switch(self):
        self.write(three_prompts().data())
        self.scan()
        changed = [e for e in self.storage.iter_events() if e["kind"] == "model_changed"]
        self.assertEqual([(e["turn_id"], e["data"]["from"], e["data"]["to"]) for e in changed],
                         [("t3", "gpt-6-luna", "gpt-6.1-sol")])

    def test_scenario4_appended_content_is_read_incrementally(self):
        data = three_prompts().data()
        half = data.index(b"\n", len(data) // 2) + 1
        self.write(data[:half])
        first = self.scan()[0].new_events
        self.append(data[half:])
        [res] = self.scan()
        self.assertEqual(first + res.new_events, len(self.keys()))
        self.assertGreater(res.new_events, 0)
        self.assertEqual(self.bodies(), self.reference(data))

    def test_scenario5_restart_resumes_without_duplicates(self):
        r = three_prompts()
        data = r.data()
        cut = data.index(b'"task_started"', len(data) // 2)
        cut = data.rindex(b"\n", 0, cut) + 1 + 40  # stop inside a line, mid-turn
        self.write(data[:cut])
        self.scan()
        self.storage.close()

        self.storage = Storage(self.db_path)  # "restart"
        self.append(data[cut:])
        self.scan()
        self.scan()
        keys = self.keys()
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(self.bodies(), self.reference(data))

    def test_scenario6_partial_line_and_bad_records(self):
        r = Rollout().meta().simple_turn("t1", "a", "b")
        lines = r.lines()
        lines.insert(3, b"{oops")
        data = b"\n".join(lines) + b"\n"
        self.write(data + b'{"timestamp": "2026-10-28T12:00:09.000Z", "ordinal": 99')
        [res] = self.scan()
        self.assertEqual(res.pending, len(b'{"timestamp": "2026-10-28T12:00:09.000Z", "ordinal": 99'))
        self.scan()  # unchanged file: nothing re-read, no duplicate issues
        issues = self.storage.issues(SID)
        self.assertEqual([i["code"] for i in issues], ["bad_json"])

        self.append(b', "type": "mystery", "payload": {}}\n')
        [res] = self.scan()
        self.assertEqual(res.pending, 0)
        codes = [i["code"] for i in self.storage.issues(SID)]
        self.assertEqual(codes, ["bad_json", "ordinal_gap"])
        self.assertEqual(self.storage.skipped(SID)["unrecognized:mystery"], 1)


class IncrementalTest(ScanCase):
    def test_any_byte_split_with_restarts_matches_a_single_pass(self):
        data = three_prompts().data()
        expected = self.reference(data)
        rng = random.Random(7)
        pos, step = 0, 0
        self.write(b"")
        while pos < len(data):
            n = rng.randint(1, 400)
            self.append(data[pos:pos + n])
            pos += n
            step += 1
            if step % 5 == 0:
                self.storage.close()
                self.storage = Storage(self.db_path)
            self.scan(max_bytes=64, batch_lines=3)
        self.assertEqual(self.bodies(), expected)

    def test_unchanged_file_is_not_reread(self):
        self.write(three_prompts().data())
        self.scan()
        with mock.patch.object(scanner, "read_complete_lines", side_effect=AssertionError("re-read")):
            [res] = self.scan()
        self.assertEqual((res.status, res.new_events), ("unchanged", 0))

    def test_failed_commit_rolls_back_and_next_scan_recovers(self):
        self.write(three_prompts().data())
        with mock.patch.object(Storage, "_add_skipped", side_effect=sqlite3.OperationalError("disk I/O")):
            [res] = self.scan()
        self.assertEqual(res.status, "error")
        self.assertEqual(self.keys(), [])
        self.assertIsNone(self.storage.file_state(str(self.sessions / file_name(SID))))
        self.assertEqual([i["code"] for i in self.storage.issues(SID)], ["store_failed"])
        self.scan()
        self.assertEqual(self.bodies(), self.reference(three_prompts().data()))

    def test_commit_refuses_stale_offset(self):
        self.write(three_prompts().data())
        self.scan()
        path = str(self.sessions / file_name(SID))
        state = self.storage.file_state(path)
        self.assertIsNone(self.storage.commit_batch(path, 0, state, [], [], {}))

    def test_replaced_file_is_reparsed_from_scratch(self):
        self.write(three_prompts().data())
        self.scan()
        smaller = Rollout().meta().simple_turn("t9", "新的", "内容")
        self.write(smaller.data())
        [res] = self.scan()
        self.assertEqual(res.status, "rewritten")
        prompts = [e["data"]["text"] for e in self.storage.iter_events() if e["kind"] == "user_prompt"]
        self.assertEqual(prompts, ["新的"])
        self.assertEqual([i["code"] for i in self.storage.issues(SID)], ["file_rewritten"])

    def test_same_size_rewrite_is_detected_by_head_fingerprint(self):
        data = three_prompts().data()
        self.write(data[:200] + b"\n")
        self.scan()
        changed = bytearray(data[:200] + b"\n")
        changed[30] = ord("X") if changed[30] != ord("X") else ord("Y")
        path = self.write(bytes(changed))
        os.utime(path, ns=(1, 1))
        [res] = self.scan()
        self.assertEqual(res.status, "rewritten")

    def test_source_files_are_never_modified(self):
        p1 = self.write(three_prompts().data(), SID)
        p2 = self.write(Rollout(SID2).meta().simple_turn("x", "y", "z").data() + b"{partial", SID2)
        before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), os.stat(p).st_mtime_ns) for p in (p1, p2)}
        self.scan()
        self.scan()
        after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), os.stat(p).st_mtime_ns) for p in (p1, p2)}
        self.assertEqual(before, after)
        self.assertEqual(sorted(os.listdir(self.sessions)), sorted([file_name(SID), file_name(SID2)]))

    def test_vanished_file_is_reported_not_raised(self):
        path = self.write(three_prompts().data())
        os.remove(path)
        res = scanner.ingest_file(self.storage, path)
        self.assertEqual(res.status, "error")


class StorageTest(ScanCase):
    def test_purge_session_removes_rows_and_its_exports(self):
        self.write(three_prompts().data(), SID)
        self.write(Rollout(SID2).meta().simple_turn("x", "y", "z").data(), SID2)
        self.scan()
        export = self.tmp / "out" / "export.jsonl"
        export.write_text("x")
        self.storage.record_export(export, [SID], 3)
        removed = self.storage.purge_session(SID)
        self.assertEqual(removed, [str(export)])
        self.assertFalse(export.exists())
        self.assertEqual(self.storage.sessions(), [SID2])
        self.assertEqual(self.storage.issues(SID), [])

    def test_parser_context_survives_restart(self):
        r = Rollout().meta().start("t1", model="gpt-q")
        self.write(r.data())
        self.scan()
        state = self.storage.file_state(str(self.sessions / file_name(SID)))
        ctx = parser.Context.from_json(state.ctx_json)
        self.assertEqual((ctx.turn_id, ctx.turn_models), ("t1", {"t1": "gpt-q"}))


if __name__ == "__main__":
    unittest.main()
