"""The collector endpoints of docs/collector-api.md, served by scripts/mock_sync_server.py.

What a client that outlives the plugin needs: read the service state, beat every 10 minutes,
terminate (the user's "stop recording"), and report uninstall — after which the device is gone
for good and uploads answer 403 `device_revoked` instead of looping.
"""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from sync_helpers import MockSync  # inserts scripts/ into sys.path
import mock_sync_server  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CLIENT = "codex-companion-collector/1.0.0"


def heartbeat(**kw):
    body = {"client": CLIENT, "queue_depth": 12, "uploaded_total": 340,
            "tiers": {"L0": 120, "L1": 80, "L2": 140, "L3": 0}, "service_state": "active"}
    body.update(kw)
    return body


def batch(*events):
    return {"schema": "ctr.sync.batch/v1", "batch_id": "b1", "client": CLIENT, "events": list(events)}


def event(eid="s1:1:0", seq=1000):
    return {"schema": "csr.event/v1", "event_id": eid, "seq": seq, "session_id": "s1",
            "kind": "user_prompt", "recorded_at": "2026-11-01T10:00:00.000Z", "data": {"text": "hi"}}


class CollectorMock(MockSync):
    """The in-process mock backend plus a request helper that can carry the device token."""

    def call(self, method, path, body=None, token=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def register(self):
        status, body = self.call("POST", "/v1/devices", {
            "client": CLIENT, "platform": "windows",
            "consent": {"version": 3, "accepted_at": "2026-11-01T10:00:00.000Z"}})
        assert status == 201, (status, body)
        return body["device_token"], body["device_id"]


class ServiceStateTest(unittest.TestCase):
    def setUp(self):
        self.mock = CollectorMock()
        self.addCleanup(self.mock.stop)
        self.token, self.device = self.mock.register()

    def test_active_until_terminated(self):
        status, body = self.mock.call("GET", "/v1/service-state", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "active")
        self.assertEqual(body["terms_version"], 4)  # mirrors collector/contract.py
        self.assertRegex(body["terms_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(body["grace_hours"], 72)
        self.assertIsNone(body["terminated_at"])
        self.assertRegex(body["server_time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_terminate_is_idempotent_and_visible_afterwards(self):
        clock = (f"2026-11-01T10:00:{s:02d}Z" for s in range(60))  # every _now() call moves it on a second
        with mock.patch.object(mock_sync_server, "_now", lambda: next(clock)):
            first = self.mock.call("POST", "/v1/service-state/terminate", {}, self.token)
            second = self.mock.call("POST", "/v1/service-state/terminate", {}, self.token)
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["state"], "terminated")
        self.assertEqual(second, first)  # the clock moved on; the termination time did not
        status, body = self.mock.call("GET", "/v1/service-state", token=self.token)
        self.assertEqual((status, body["state"], body["terminated_at"]),
                         (200, "terminated", first[1]["terminated_at"]))

    def test_termination_is_per_device(self):
        other, other_id = self.mock.register()
        self.mock.call("POST", "/v1/service-state/terminate", {}, self.token)
        self.assertEqual(self.mock.call("GET", "/v1/service-state", token=other)[1]["state"], "active")
        view = self.mock.call("GET", "/_mock/service-state")[1]["service_state"]
        self.assertEqual(view[self.device]["state"], "terminated")
        self.assertIsNotNone(view[self.device]["terminated_at"])
        self.assertEqual(view[other_id]["state"], "active")
        self.assertIsNone(view[other_id]["terminated_at"])


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.mock = CollectorMock()
        self.addCleanup(self.mock.stop)
        self.token, self.device = self.mock.register()

    def test_only_the_latest_beat_per_device_is_kept(self):
        status, body = self.mock.call("POST", "/v1/device/heartbeat", heartbeat(), self.token)
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        self.assertIn("server_time", body)
        self.mock.call("POST", "/v1/device/heartbeat", heartbeat(queue_depth=0, uploaded_total=341), self.token)
        beat = self.mock.call("GET", "/_mock/heartbeats")[1]["heartbeats"][self.device]
        self.assertEqual(beat["count"], 2)
        self.assertEqual(beat["heartbeat"]["queue_depth"], 0)
        self.assertEqual(beat["heartbeat"]["uploaded_total"], 341)
        self.assertEqual(beat["heartbeat"]["tiers"], {"L0": 120, "L1": 80, "L2": 140, "L3": 0})

    def test_body_over_4_kib_is_413(self):
        raw = json.dumps(heartbeat(pad="x" * 4200)).encode("utf-8")
        status, body = self.mock.call("POST", "/v1/device/heartbeat", token=self.token, raw=raw)
        self.assertEqual((status, body), (413, {"error": {"code": "too_large"}}))
        self.assertEqual(self.mock.call("GET", "/_mock/heartbeats")[1]["heartbeats"], {})

    def test_garbage_body_is_400(self):
        status, body = self.mock.call("POST", "/v1/device/heartbeat", token=self.token, raw=b"not json")
        self.assertEqual((status, body), (400, {"error": {"code": "invalid_request"}}))

    def test_unknown_tokens(self):
        self.assertEqual(self.mock.call("GET", "/v1/service-state")[0], 401)
        status, body = self.mock.call("GET", "/v1/service-state", token="ctrd_nope")
        self.assertEqual((status, body), (401, {"error": {"code": "unauthorized"}}))
        self.assertEqual(self.mock.call("POST", "/v1/device/heartbeat", heartbeat(), "ctrd_nope")[0], 401)
        # the recorder endpoints keep the code docs/sync-api.md §1 promises
        self.assertEqual(self.mock.call("GET", "/v1/devices/me", token="ctrd_nope")[1]["error"]["code"],
                         "device_invalid")


class UninstallTest(unittest.TestCase):
    def setUp(self):
        self.mock = CollectorMock()
        self.addCleanup(self.mock.stop)
        self.token, self.device = self.mock.register()

    def test_the_device_is_gone_for_good(self):
        self.assertEqual(self.mock.call("POST", "/v1/events/batch", batch(event()), self.token)[1]["accepted"], 1)
        self.assertEqual(self.mock.call("POST", "/v1/device/uninstall", None, self.token), (200, {"ok": True}))
        self.assertEqual(self.mock.call("POST", "/v1/device/uninstall", None, self.token), (200, {"ok": True}))
        # uploading after uninstall is a permanent answer, not a retryable one
        status, body = self.mock.call("POST", "/v1/events/batch", batch(event("s1:1:1", 1001)), self.token)
        self.assertEqual((status, body), (403, {"error": {"code": "device_revoked"}}))
        self.assertEqual(self.mock.call("GET", "/v1/service-state", token=self.token)[0], 403)
        self.assertEqual(self.mock.call("POST", "/v1/device/heartbeat", heartbeat(), self.token)[0], 403)
        uninstalls = self.mock.call("GET", "/_mock/uninstalls")[1]["uninstalls"]
        self.assertEqual([(u["device_id"], u["calls"]) for u in uninstalls], [(self.device, 2)])
        self.assertRegex(uninstalls[0]["uninstalled_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        # the events uploaded before the uninstall are still there
        self.assertEqual(self.mock.call("GET", "/_mock/events")[1]["events"][0]["event_id"], "s1:1:0")

    def test_uninstall_without_a_token_is_401(self):
        self.assertEqual(self.mock.call("POST", "/v1/device/uninstall")[1]["error"]["code"], "unauthorized")


class StoreTest(unittest.TestCase):
    def test_service_state_and_heartbeats_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "mock.json"
            first = CollectorMock(store)
            token, device = first.register()
            first.call("POST", "/v1/service-state/terminate", {}, token)
            first.call("POST", "/v1/device/heartbeat", heartbeat(), token)
            first.stop()
            again = CollectorMock(store)
            try:
                self.assertEqual(again.call("GET", "/v1/service-state", token=token)[1]["state"], "terminated")
                self.assertEqual(again.call("GET", "/_mock/heartbeats")[1]["heartbeats"][device]
                                 ["heartbeat"]["queue_depth"], 12)
            finally:
                again.stop()


if __name__ == "__main__":
    unittest.main()
