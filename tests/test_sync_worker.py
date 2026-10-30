import json
import unittest
from collections import Counter
from datetime import timedelta

from ctr import store
from ctr.sync import api
from ctr.sync import config as sconfig
from ctr.sync import outbox
from ctr.sync import state as sstate
from ctr.sync import vault
from ctr.sync.worker import Worker

from helpers import T0
from sync_helpers import MockSync, SyncCase


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class WorkerCase(SyncCase):
    def setUp(self):
        super().setUp()
        self.mock = MockSync()
        self.sync_on(at=-60, base_url=self.mock.url)
        self.time = FakeTime()

    def tearDown(self):
        self.mock.stop()
        super().tearDown()

    def worker(self, **kw):
        kw.setdefault("clock", self.time.clock)
        kw.setdefault("sleep", self.time.sleep)
        kw.setdefault("rng", lambda: 0.5)  # no jitter
        return Worker(self.paths, **kw)

    def record_turn(self, n=3):
        self.hook("UserPromptSubmit", prompt="hello")
        for i in range(n):
            self.hook("PreToolUse", tool_name="Bash", tool_use_id=f"exec-{i}", tool_input={"command": "dir"})
            self.hook("PostToolUse", tool_name="Bash", tool_use_id=f"exec-{i}", tool_input={"command": "dir"},
                      tool_response="ok")
        self.hook("Stop", last_assistant_message="done", stop_hook_active=False)

    def local_ids(self):
        return [e["event_id"] for e in self.events()]

    def remote_ids(self):
        return [e["event_id"] for e in self.mock.events()]

    def status(self):
        return json.loads(self.sp.status.read_text(encoding="utf-8"))


class UploadTest(WorkerCase):
    def test_registers_and_uploads_every_event_in_order(self):
        self.record_turn()
        self.assertEqual(self.worker().run(once=True), "idle")
        self.assertEqual(self.remote_ids(), self.local_ids())
        remote = self.mock.events()
        self.assertEqual([e["seq"] for e in remote], list(range(1, len(remote) + 1)))
        dev = vault.load(self.sp.device)
        self.assertTrue(dev["device_token"].startswith("ctrd_"))
        self.assertEqual(self.status()["device_id"], dev["device_id"])
        self.assertIsNotNone(self.status()["last_success_at"])

    def test_second_run_sends_only_new_events(self):
        self.record_turn(1)
        self.worker().run(once=True)
        self.hook("UserPromptSubmit", prompt="more", turn_id="t2")
        self.worker().run(once=True)
        self.assertEqual(self.remote_ids(), self.local_ids())
        self.assertEqual(sum(s["duplicates"] for s in self.mock.state.stats.values()), 0)

    def test_lost_response_is_resent_and_deduplicated(self):
        self.record_turn()
        self.mock.fail_next(mode="drop_after_store")
        self.assertEqual(self.worker().run(once=True), "failed")
        self.assertEqual(self.worker().run(once=True), "idle")
        ids = self.remote_ids()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids), sorted(self.local_ids()))
        self.assertGreater(sum(s["duplicates"] for s in self.mock.state.stats.values()), 0)

    def test_backend_errors_back_off_then_succeed(self):
        self.record_turn(1)
        self.mock.fail_next(status=503, count=2)
        w = self.worker(idle_exit_s=5)
        self.assertEqual(w.run(), "idle")
        self.assertEqual(w.waits[:2], [2.0, 4.0])
        self.assertEqual(sorted(self.remote_ids()), sorted(self.local_ids()))

    def test_rate_limit_waits_retry_after_within_bounds(self):
        self.record_turn(1)
        self.mock.fail_next(status=429, retry_after=7)
        self.mock.fail_next(status=429, retry_after=0)
        self.mock.fail_next(status=429, retry_after=10**6)
        w = self.worker(idle_exit_s=5)
        w.run()
        self.assertEqual(w.waits[:3], [7, 2.0, 300.0])

    def test_registration_rejected_keeps_the_whole_queue(self):
        self.record_turn(1)
        self.mock.fail_next(status=400, code="invalid_request", path="/v1/devices")
        self.assertEqual(self.worker().run(once=True), "failed")
        totals = outbox.Outbox(self.paths).totals()
        self.assertEqual((totals["uploaded"], totals["rejected"]), (0, 0))
        self.assertEqual(self.worker().run(once=True), "idle")
        self.assertEqual(sorted(self.remote_ids()), sorted(self.local_ids()))

    def test_a_400_that_is_not_about_the_batch_backs_off(self):
        self.register()
        self.record_turn(1)
        self.mock.fail_next(status=400, path="/v1/events/batch")  # e.g. a proxy's HTML 400
        self.assertEqual(self.worker().run(once=True), "failed")
        self.assertEqual(outbox.Outbox(self.paths).totals()["rejected"], 0)
        self.assertEqual(self.worker().run(once=True), "idle")
        self.assertEqual(sorted(self.remote_ids()), sorted(self.local_ids()))

    def test_sync_switched_off_while_a_batch_is_built_registers_nothing(self):
        self.record_turn(1)

        def factory(url, timeout):
            sstate.revoke(self.sp, T0 + timedelta(days=1), delete_remote=False)  # user revokes meanwhile
            return api.Api(url, timeout)

        self.assertEqual(self.worker(api_factory=factory).run(once=True), "disabled")
        self.assertEqual(self.mock.state.requests, [])

    def register(self):
        self.hook("UserPromptSubmit", prompt="seed", turn_id="t0")
        self.assertEqual(self.worker().run(once=True), "idle")
        return outbox.Outbox(self.paths).totals()["uploaded"]

    def test_too_large_batch_is_halved(self):
        self.register()
        self.record_turn(3)
        self.mock.fail_next(status=413)
        self.assertEqual(self.worker().run(once=True), "idle")
        self.assertEqual(sorted(self.remote_ids()), sorted(self.local_ids()))

    def test_event_the_backend_cannot_accept_is_skipped_after_isolating_it(self):
        uploaded = self.register()
        self.hook("UserPromptSubmit", prompt="one")
        self.mock.fail_next(status=400, code="invalid_batch", count=50)
        self.assertEqual(self.worker().run(once=True), "idle")
        self.assertEqual(outbox.Outbox(self.paths).totals()["rejected"], len(self.local_ids()) - uploaded)

    def test_incomplete_results_do_not_move_the_cursor(self):
        self.record_turn(1)

        class Partial:
            base_url = self.mock.url

            def register(self, *a):
                return {"device_id": "dev_x", "device_token": "ctrd_xxxxxxxxxx"}

            def upload(self, token, batch):
                return {"results": [{"event_id": batch["events"][0]["event_id"], "status": "accepted"}]}

        self.assertEqual(self.worker(api_factory=lambda url, timeout: Partial()).run(once=True), "failed")
        self.assertEqual(outbox.Outbox(self.paths).totals()["uploaded"], 0)

    def test_unknown_device_registers_again(self):
        self.record_turn(1)
        self.worker().run(once=True)
        first = vault.load(self.sp.device)["device_id"]
        self.mock.state.devices.clear()  # the backend forgot the device
        self.hook("UserPromptSubmit", prompt="again", turn_id="t2")
        self.assertEqual(self.worker(idle_exit_s=5).run(), "idle")
        self.assertNotEqual(vault.load(self.sp.device)["device_id"], first)
        self.assertEqual(sorted(self.remote_ids()), sorted(self.local_ids()))

    def test_device_revoked_by_backend_turns_sync_off(self):
        self.record_turn(1)
        self.worker().run(once=True)
        self.mock.call("POST", "/_mock/revoke-all", {})
        self.hook("UserPromptSubmit", prompt="again", turn_id="t2")
        self.assertEqual(self.worker().run(once=True), "device_revoked")
        st = sstate.load(self.sp)
        self.assertFalse(sstate.is_on(st))
        self.assertEqual(st.stopped_reason, "device_revoked")


class LifecycleTest(WorkerCase):
    def test_paused_worker_uploads_nothing(self):
        self.record_turn(1)
        self.sync_pause(at=10**6)
        self.assertEqual(self.worker().run(once=True), "disabled")
        self.assertEqual(self.remote_ids(), [])

    def test_not_configured(self):
        sconfig.save(self.sp, sconfig.Config(base_url=None))
        self.record_turn(1)
        self.assertEqual(self.worker().run(once=True), "not_configured")

    def test_pausing_wakes_an_uploader_that_is_backing_off(self):
        self.record_turn(1)
        self.mock.fail_next(status=503, count=10**6)
        sleep = self.time.sleep

        def sleep_then_pause(seconds):
            sleep(seconds)
            if self.time.now >= 40:  # mid-way through a 32 s backoff wait
                self.sync_pause(at=10**6)

        self.assertEqual(self.worker(sleep=sleep_then_pause).run(), "disabled")
        self.assertLess(self.time.now, 45, "exits within one poll of the pause, not after the full wait")

    def test_hand_edited_backend_is_not_used_without_consent(self):
        self.record_turn(1)
        sconfig.save(self.sp, sconfig.Config(base_url="http://127.0.0.1:9"))
        self.assertEqual(self.worker().run(once=True), "not_configured")
        self.assertEqual(self.status()["last_error"]["kind"], "backend_changed")

    def test_single_instance(self):
        fd = store.try_hold(self.sp.worker_lock)
        try:
            self.assertEqual(self.worker().run(once=True), "already_running")
        finally:
            store.release(fd)

    def test_idle_worker_exits_after_idle_period(self):
        self.assertEqual(self.worker(idle_exit_s=10, poll_s=2).run(), "idle")
        self.assertEqual(sum(self.time.sleeps), 10)

    def test_gives_up_after_failing_for_an_hour(self):
        self.record_turn(1)
        self.mock.fail_next(status=503, count=10**6)
        w = self.worker(fail_exit_s=3600)
        self.assertEqual(w.run(), "failing")
        self.assertGreaterEqual(self.time.now, 3600)
        self.assertLessEqual(max(w.waits), 300)
        self.assertLessEqual(max(self.time.sleeps), 2, "waits are taken in poll-sized steps")
        self.assertEqual(self.status()["last_error"]["kind"], "register_http_503")


class RemoteOpsTest(WorkerCase):
    def test_revoke_with_delete_removes_remote_data_then_the_device(self):
        self.record_turn(1)
        self.worker().run(once=True)
        self.assertTrue(self.remote_ids())
        sstate.revoke(self.sp, T0 + timedelta(days=1), delete_remote=True)
        self.assertEqual(self.worker().run(once=True), "disabled")
        self.assertEqual(self.remote_ids(), [])
        self.assertEqual(self.mock.state.devices, {})
        self.assertEqual(sstate.load(self.sp).pending_ops, [])
        self.assertIsNone(vault.load(self.sp.device))
        deletes = [p for m, p in self.mock.state.requests if m == "DELETE"]
        self.assertEqual(deletes, ["/v1/devices/me/data", "/v1/devices/me"])

    def test_revoke_keeps_remote_data_when_asked(self):
        self.record_turn(1)
        self.worker().run(once=True)
        sstate.revoke(self.sp, T0 + timedelta(days=1), delete_remote=False)
        self.worker().run(once=True)
        self.assertTrue(self.remote_ids())
        self.assertEqual(self.mock.state.devices, {})

    def test_ops_wait_for_the_network(self):
        self.record_turn(1)
        self.worker().run(once=True)
        sstate.revoke(self.sp, T0 + timedelta(days=1), delete_remote=True)
        dev = vault.load(self.sp.device)
        vault.save(self.sp.device, {**dev, "base_url": "http://127.0.0.1:9"})  # unreachable
        self.assertEqual(self.worker().run(once=True), "failed")
        self.assertEqual(sstate.load(self.sp).pending_ops, ["delete_remote", "revoke_device"])
        vault.save(self.sp.device, dev)
        self.worker().run(once=True)
        self.assertEqual(sstate.load(self.sp).pending_ops, [])
        self.assertEqual(self.remote_ids(), [])

    def test_permanent_op_errors_do_not_block_sync_forever(self):
        self.record_turn(1)
        self.worker().run(once=True)
        sstate.revoke(self.sp, T0 + timedelta(days=1), delete_remote=True)
        self.mock.fail_next(status=404, path="/v1/devices/me/data")
        w = self.worker()
        self.assertEqual(w.run_ops(), "progress")
        self.assertEqual(w.op_results, {"delete_remote": "rejected_404", "revoke_device": "ok"})
        self.assertEqual(sstate.load(self.sp).pending_ops, [])
        self.assertEqual(self.status()["last_op_error"]["kind"], "delete_remote_rejected_404")

    def test_ops_retry_on_server_errors(self):
        self.record_turn(1)
        self.worker().run(once=True)
        sstate.revoke(self.sp, T0 + timedelta(days=1), delete_remote=True)
        self.mock.fail_next(status=503, path="/v1/devices/me/data")
        self.assertEqual(self.worker().run_ops(), "failed")
        self.assertEqual(sstate.load(self.sp).pending_ops, ["delete_remote", "revoke_device"])

    def test_ops_without_a_device_complete_at_once(self):
        sstate.revoke(self.sp, T0 + timedelta(days=1), delete_remote=True)
        self.assertEqual(self.worker().run(once=True), "disabled")
        self.assertEqual(sstate.load(self.sp).pending_ops, [])
        self.assertEqual(self.mock.state.requests, [])


class RestartTest(SyncCase):
    def test_backend_down_then_back_creates_no_duplicates(self):
        store_file = self.tmp / "mock.json"
        mock = MockSync(store=store_file)
        port = mock.port
        self.sync_on(at=-60, base_url=mock.url)
        time = FakeTime()
        w = lambda: Worker(self.paths, clock=time.clock, sleep=time.sleep, rng=lambda: 0.5)  # noqa: E731
        self.hook("UserPromptSubmit", prompt="first")
        self.assertEqual(w().run(once=True), "idle")
        mock.stop()
        self.hook("UserPromptSubmit", prompt="while down", turn_id="t2")
        self.assertEqual(w().run(once=True), "failed")
        mock = MockSync(store=store_file, port=port)
        try:
            self.assertEqual(w().run(once=True), "idle")
            ids = [e["event_id"] for e in mock.events()]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(sorted(ids), sorted(e["event_id"] for e in self.events()))
            self.assertEqual(Counter(s["duplicates"] for s in mock.state.stats.values()), Counter({0: 1}))
        finally:
            mock.stop()


if __name__ == "__main__":
    unittest.main()
