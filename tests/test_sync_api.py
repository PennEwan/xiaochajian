import socket
import unittest

from ctr.sync import api

from sync_helpers import MockSync


def event(eid, seq=1, sid="s1"):
    return {"schema": "ctr.event/v2", "event_id": eid, "seq": seq, "session_id": sid, "kind": "user_prompt",
            "recorded_at": "2026-10-28T10:00:00.000Z", "data": {"text": "hi"}}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.mock = MockSync()
        self.api = api.Api(self.mock.url, timeout=5)
        self.token = self.api.register(1, "2026-10-28T10:00:00.000Z")["device_token"]

    def tearDown(self):
        self.mock.stop()

    def batch(self, *events):
        return {"schema": "ctr.sync.batch/v1", "batch_id": "b1", "client": api.CLIENT, "events": list(events)}

    def test_register_gives_a_ctrd_token_and_sends_client_header(self):
        self.assertTrue(self.token.startswith("ctrd_"))
        self.assertIn(api.CLIENT, self.mock.state.client_headers)
        me = self.api.me(self.token)
        self.assertTrue(me["device_id"].startswith("dev_"))
        self.assertEqual(me["event_count"], 0)

    def test_upload_is_idempotent_by_event_id(self):
        first = self.api.upload(self.token, self.batch(event("e1"), event("e2", 2)))
        self.assertEqual((first["accepted"], first["duplicates"]), (2, 0))
        again = self.api.upload(self.token, self.batch(event("e2", 2), event("e3", 3)))
        self.assertEqual([r["status"] for r in again["results"]], ["duplicate", "accepted"])
        self.assertEqual(self.api.me(self.token)["event_count"], 3)

    def test_invalid_event_is_rejected_individually(self):
        res = self.api.upload(self.token, self.batch(event("e1"), {"event_id": "bad"}))
        self.assertEqual([r["status"] for r in res["results"]], ["accepted", "rejected"])

    def test_unknown_token_is_401_and_revoked_device_is_403(self):
        with self.assertRaises(api.HttpError) as cm:
            self.api.me("ctrd_nope")
        self.assertEqual((cm.exception.status, cm.exception.code), (401, "device_invalid"))
        self.mock.call("POST", "/_mock/revoke-all", {})
        with self.assertRaises(api.HttpError) as cm:
            self.api.upload(self.token, self.batch(event("e1")))
        self.assertEqual((cm.exception.status, cm.exception.code), (403, "device_revoked"))

    def test_server_errors_and_rate_limits(self):
        self.mock.fail_next(status=503)
        with self.assertRaises(api.HttpError) as cm:
            self.api.upload(self.token, self.batch(event("e1")))
        self.assertEqual(cm.exception.status, 503)
        self.mock.fail_next(status=429)
        with self.assertRaises(api.HttpError) as cm:
            self.api.upload(self.token, self.batch(event("e1")))
        self.assertEqual((cm.exception.code, cm.exception.retry_after), ("rate_limited", 1))

    def test_response_lost_after_store_is_unknown_and_resend_is_duplicate(self):
        self.mock.fail_next(mode="drop_after_store")
        with self.assertRaises(api.Unknown):
            self.api.upload(self.token, self.batch(event("e1")))
        res = self.api.upload(self.token, self.batch(event("e1")))
        self.assertEqual(res["results"][0]["status"], "duplicate")

    def test_redirect_is_not_followed(self):
        self.mock.fail_next(status=307, location="http://127.0.0.1:9/steal")
        with self.assertRaises(api.Unknown) as cm:
            self.api.upload(self.token, self.batch(event("e1")))
        self.assertEqual(cm.exception.kind, "redirect")

    def test_too_large_body(self):
        big = event("e1")
        big["data"]["text"] = "x" * (4 * 1024 * 1024 + 10)
        with self.assertRaises(api.HttpError) as cm:
            self.api.upload(self.token, self.batch(big))
        self.assertEqual((cm.exception.status, cm.exception.code), (413, "batch_too_large"))

    def test_delete_data_then_revoke(self):
        self.api.upload(self.token, self.batch(event("e1"), event("e2", 2)))
        self.assertEqual(self.api.delete_data(self.token)["deleted_events"], 2)
        self.assertEqual(self.api.revoke(self.token), {"revoked": True})
        with self.assertRaises(api.HttpError) as cm:
            self.api.revoke(self.token)
        self.assertEqual(cm.exception.status, 401)


class ConnectionTest(unittest.TestCase):
    def test_refused_is_not_sent(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(api.NotSent):
            api.Api(f"http://127.0.0.1:{port}", timeout=5).me("ctrd_x")

    def test_insecure_urls_are_refused(self):
        for url in ["http://sync.example.com", "ftp://x", "", "https://"]:
            with self.assertRaises(api.InsecureUrl, msg=url):
                api.Api(url)
        api.Api("https://sync.example.com/")
        api.Api("http://localhost:18788")


if __name__ == "__main__":
    unittest.main()
