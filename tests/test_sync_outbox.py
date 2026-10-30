import json
import unittest
from datetime import timedelta

from ctr.sync import outbox
from ctr.sync import state as sstate

from helpers import T0, rec, ts, write_rollout
from sync_helpers import SID, SyncCase


def v2_line(eid, seq, at, data=None, kind="user_prompt"):
    return {"schema": "ctr.event/v2", "event_id": eid, "seq": seq, "kind": kind, "recorded_at": ts(at),
            "source_ts": None, "session_id": SID, "source": {"type": "hook"}, "redaction": {"applied": False,
                                                                                         "counts": {}},
            "completeness": {"status": "complete"}, "data": data or {"text": "x"}}


class OutboxCase(SyncCase):
    def setUp(self):
        super().setUp()
        self.box = outbox.Outbox(self.paths)

    def st(self):
        return sstate.load(self.sp)

    def drain(self, **limits):
        """Every batch until empty, committing each as fully accepted."""
        batches = []
        while True:
            batch = self.box.next_batch(self.st(), **limits)
            if batch is None:
                return batches
            self.box.commit(batch, {e["event_id"]: "accepted" for e in batch.events})
            batches.append(batch)

    def uploaded(self, **limits):
        return [e for b in self.drain(**limits) for e in b.events]


class WindowTest(OutboxCase):
    def test_only_events_recorded_inside_the_sync_window(self):
        with self.clock(-30):
            self.hook("UserPromptSubmit", prompt="before sync")
        self.sync_on(at=0)
        with self.clock(10):
            self.hook("UserPromptSubmit", prompt="after sync")
        prompts = [e["data"]["text"] for e in self.uploaded() if e["kind"] == "user_prompt"]
        self.assertEqual(prompts, ["after sync"])
        self.assertIsNone(self.box.next_batch(self.st()), "skipped lines are consumed too")
        cursor = json.loads(next(self.sp.cursors.glob("*.json")).read_text(encoding="utf-8"))
        self.assertGreater(cursor["skipped"]["outside_sync_window"], 0)

    def test_paused_period_is_never_uploaded(self):
        self.sync_on(at=0)
        self.sync_pause(at=20)
        with self.clock(30):
            self.hook("UserPromptSubmit", prompt="while paused")
        self.sync_on(at=40)
        with self.clock(50):
            self.hook("UserPromptSubmit", prompt="resumed")
        prompts = [e["data"]["text"] for e in self.uploaded() if e["kind"] == "user_prompt"]
        self.assertEqual(prompts, ["resumed"])

    def test_paused_state_uploads_nothing_but_keeps_the_queue(self):
        self.sync_on(at=0)
        with self.clock(10):
            self.hook("UserPromptSubmit", prompt="queued")
        self.sync_pause(at=20)
        self.assertIsNone(self.box.next_batch(self.st()))
        self.sync_on(at=30)
        prompts = [e["data"]["text"] for e in self.uploaded() if e["kind"] == "user_prompt"]
        self.assertEqual(prompts, ["queued"], "authorised before the pause, uploaded after resuming")

    def test_transcript_content_from_before_sync_is_excluded(self):
        write_rollout(self.rollout, [
            rec(-20, "event_msg", {"type": "item_completed", "turn_id": "t0",
                                   "item": {"type": "UserMessage", "id": "u0", "content": [{"type": "text",
                                                                                           "text": "old"}]}}),
            rec(15, "event_msg", {"type": "item_completed", "turn_id": "t1",
                                  "item": {"type": "UserMessage", "id": "u1", "content": [{"type": "text",
                                                                                          "text": "new"}]}}),
        ])
        self.sync_on(at=0)
        with self.clock(20):
            self.hook("Stop", last_assistant_message="ok", stop_hook_active=False)
        texts = [e["data"]["text"] for e in self.uploaded()
                 if e["kind"] == "user_prompt" and e["source"]["type"] == "transcript"]
        self.assertEqual(texts, ["new"])

    def test_revoked_state_uploads_nothing(self):
        self.sync_on(at=0)
        with self.clock(10):
            self.hook("UserPromptSubmit", prompt="queued")
        sstate.revoke(self.sp, T0 + timedelta(seconds=20), delete_remote=False)
        self.assertIsNone(self.box.next_batch(self.st()))

    def test_v1_events_are_skipped_as_old_schema(self):
        self.sync_on(at=0)
        with self.clock(10):
            self.hook("UserPromptSubmit", prompt="v2")
        old = v2_line("old-1", 1, 11)
        old["schema"], old["seq"] = "ctr.event/v1", None
        self.append_raw([old])
        ids = [e["event_id"] for e in self.uploaded()]
        self.assertNotIn("old-1", ids)
        cursor = json.loads(next(self.sp.cursors.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(cursor["skipped"]["old_schema"], 1)


class BatchTest(OutboxCase):
    def setUp(self):
        super().setUp()
        self.sync_on(at=0)
        with self.clock(5):
            self.hook("UserPromptSubmit", prompt="seed")  # creates the session file and cursor

    def test_batches_respect_limits_and_keep_seq_order(self):
        with self.clock(10):
            for i in range(6):
                self.hook("UserPromptSubmit", prompt=f"p{i}")
        batches = self.drain(max_events=4)
        self.assertTrue(all(len(b.events) <= 4 for b in batches))
        seqs = [e["seq"] for b in batches for e in b.events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_an_oversized_event_goes_alone(self):
        self.append_raw([v2_line("big", 100, 11, {"text": "y" * 5000}), v2_line("small", 101, 11)])
        batches = self.drain(max_bytes=3000)
        sizes = [[e["event_id"] for e in b.events] for b in batches]
        self.assertIn(["big"], sizes)

    def test_uncommitted_batch_is_rebuilt_identically(self):
        first = self.box.next_batch(self.st())
        again = self.box.next_batch(self.st())
        self.assertEqual([e["event_id"] for e in first.events], [e["event_id"] for e in again.events])

    def test_partial_trailing_line_waits_until_complete(self):
        self.drain()
        line = json.dumps(v2_line("late", 200, 12))
        self.append_raw([line[:20]])
        self.assertIsNone(self.box.next_batch(self.st()))
        self.append_raw([line[20:] + "\n"])
        self.assertEqual([e["event_id"] for e in self.uploaded()], ["late"])

    def test_projection_strips_diagnostics_and_redacts_again(self):
        key = "sk-proj-" + "Q" * 30
        self.append_raw([v2_line("leaky", 300, 12, {"text": f"use {key}"})])
        events = {e["event_id"]: e for e in self.uploaded()}
        self.assertNotIn(key, json.dumps(events["leaky"]))
        self.assertEqual(events["leaky"]["redaction"]["upload_pass"], {"openai_key": 1})
        self.assertTrue(events["leaky"]["redaction"]["applied"])
        inv = next(e for e in events.values() if e["kind"] == "hook_invocation")
        self.assertEqual(set(inv["data"]), {"payload_keys", "transcript_path_present", "notice_emitted",
                                            "duration_ms"})

    def test_rejected_events_are_counted_and_skipped(self):
        batch = self.box.next_batch(self.st())
        self.box.commit(batch, {e["event_id"]: "rejected" for e in batch.events})
        self.assertIsNone(self.box.next_batch(self.st()))
        cursor = json.loads(next(self.sp.cursors.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(cursor["rejected"], len(batch.events))
        self.assertEqual(cursor["uploaded"], 0)

    def test_deleted_session_file_and_orphan_cursor(self):
        self.drain()
        self.session_file().unlink()
        self.assertIsNone(self.box.next_batch(self.st()))
        self.assertEqual(self.box.prune(), 1)
        self.assertEqual(list(self.sp.cursors.glob("*.json")), [])

    def test_recreated_file_that_outgrew_the_old_offset_is_read_from_the_start(self):
        self.drain()
        size = self.session_file().stat().st_size
        lines = [json.dumps(v2_line(f"again-{i}", i + 1, 12, {"text": "z" * 200})) + "\n" for i in range(30)]
        self.session_file().write_text("".join(lines), encoding="utf-8")  # purged, then recorded again
        self.assertGreater(self.session_file().stat().st_size, size)
        ids = [e["event_id"] for e in self.uploaded()]
        self.assertEqual(ids, [f"again-{i}" for i in range(30)])
        self.assertEqual(self.box.totals()["skipped:file_rewound"], 1)

    def test_lines_without_an_event_id_never_block_the_queue(self):
        no_id = v2_line("x", 50, 12)
        del no_id["event_id"]
        numeric = v2_line("y", 51, 12)
        numeric["event_id"] = 7
        self.append_raw([no_id, numeric, v2_line("ok-1", 52, 12)])
        ids = [e["event_id"] for e in self.uploaded()]
        self.assertIn("ok-1", ids)
        self.assertEqual(self.box.totals()["skipped:bad_line"], 2)

    def test_does_not_read_the_recorder_cursors(self):
        import shutil
        shutil.rmtree(self.paths.cursors)  # the uploader finds session files by listing them
        self.assertTrue(self.uploaded())

    def test_already_redacted_values_are_not_counted_again(self):
        self.append_raw([v2_line("clean", 60, 12, {"password": "[REDACTED:key]",
                                                   "cmd": "curl -H 'Authorization: [REDACTED:auth_header]' "
                                                          "https://u:[REDACTED:url_password]@x"})])
        event = next(e for e in self.uploaded() if e["event_id"] == "clean")
        self.assertNotIn("upload_pass", event["redaction"])


    def test_rewritten_shorter_file_restarts_from_zero(self):
        self.drain()
        self.session_file().write_text(json.dumps(v2_line("new-1", 1, 12)) + "\n", encoding="utf-8")
        self.assertEqual([e["event_id"] for e in self.uploaded()], ["new-1"])

    def test_backlog_counts_uploadable_events(self):
        events, sessions = self.box.backlog(self.st())
        self.assertGreater(events, 0)
        self.assertEqual(sessions, 1)
        self.drain()
        self.assertEqual(self.box.backlog(self.st()), (0, 0))


class StartFromNowTest(OutboxCase):
    def test_fresh_consent_skips_history_without_reading_it_later(self):
        with self.clock(-30):
            self.hook("UserPromptSubmit", prompt="history")
        skipped = self.box.start_from_now()
        self.assertGreater(skipped, 0)
        self.sync_on(at=0)
        with self.clock(10):
            self.hook("UserPromptSubmit", prompt="new")
        prompts = [e["data"]["text"] for e in self.uploaded() if e["kind"] == "user_prompt"]
        self.assertEqual(prompts, ["new"])
        totals = self.box.totals()
        self.assertEqual(totals["skipped:before_sync"], skipped)
        self.assertEqual(totals["skipped:outside_sync_window"], 0, "history was never parsed again")

    def test_partial_last_line_is_not_skipped_into(self):
        self.hook("UserPromptSubmit", prompt="x")
        line = json.dumps(v2_line("late", 99, 12))
        self.append_raw([line[:30]])
        self.box.start_from_now()
        cursor = json.loads(next(self.sp.cursors.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(cursor["offset"], self.session_file().stat().st_size - 30)


if __name__ == "__main__":
    unittest.main()
