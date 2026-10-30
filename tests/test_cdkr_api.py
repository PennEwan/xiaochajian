import os
import socket
import time
import unittest
from unittest import mock

from cdkr import api

from cdkr_helpers import MockBackend

ACCOUNT_A = "acct-aaaa"
ACCOUNT_B = "acct-bbbb"


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackend()
        self.api = api.Api(self.backend.url, timeout=5)

    def tearDown(self):
        self.backend.stop()

    def redeem(self, cdk, account=ACCOUNT_A, email="a@example.com"):
        return self.api.redeem(cdk, account, email)

    def assertHttpError(self, status, code, fn, *args):
        with self.assertRaises(api.HttpError) as ctx:
            fn(*args)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (status, code))
        return ctx.exception

    def test_redemption_names_the_account_and_carries_no_token(self):
        self.assertEqual(self.redeem("TEST-OK04", ACCOUNT_B, "b@example.com")["status"], "succeeded")
        self.assertIn(("TEST-OK04", ACCOUNT_B), self.backend.state.by_key)
        self.assertEqual(self.backend.state.paths, ["/v1/redemptions"])
        self.assertEqual(self.backend.state.authorizations, [""])

    def test_valid_cdk_succeeds_once_then_replays(self):
        first = self.redeem("TEST-OK01")
        self.assertEqual((first["status"], first["replayed"]), ("succeeded", False))
        self.assertTrue(first["product"])
        again = self.redeem("TEST-OK01")
        self.assertEqual((again["status"], again["replayed"]), ("succeeded", True))
        self.assertEqual(again["redemption_id"], first["redemption_id"])

    def test_cdk_used_by_another_account_conflicts(self):
        self.redeem("TEST-OK02")
        self.assertHttpError(409, "cdk_already_redeemed", self.redeem, "TEST-OK02", ACCOUNT_B, "b@example.com")

    def test_invalid_expired_and_failed_cdks(self):
        self.assertHttpError(400, "cdk_invalid", self.redeem, "NOPE-0000")
        self.assertHttpError(410, "cdk_expired", self.redeem, "TEST-EXPD")
        err = self.assertHttpError(422, "fulfillment_failed", self.redeem, "TEST-FAIL")
        self.assertTrue(err.message)

    def test_processing_then_succeeded_on_poll(self):
        first = self.redeem("TEST-PROC")
        self.assertEqual(first["status"], "processing")
        polls = [self.api.get_redemption(first["redemption_id"])["status"] for _ in range(2)]
        self.assertEqual(polls, ["processing", "succeeded"])

    def test_timeout_is_unknown_and_retry_is_idempotent(self):
        self.backend.state.slow_seconds = 1.0
        slow = api.Api(self.backend.url, timeout=0.3)
        with self.assertRaises(api.Unknown):
            slow.redeem("TEST-SLOW", ACCOUNT_A, "a@example.com")
        time.sleep(1.2)
        again = self.redeem("TEST-SLOW")
        self.assertEqual((again["status"], again["replayed"]), ("succeeded", True))
        self.assertEqual(self.backend.state.consumed("TEST-SLOW"), 1)

    def test_incomplete_account_is_400(self):
        self.assertHttpError(400, "invalid_account", self.redeem, "TEST-OK03", "")
        self.assertHttpError(400, "invalid_account", self.redeem, "TEST-OK03", ACCOUNT_A, "")

    def test_server_error_and_rate_limit(self):
        self.backend.post("/_mock/fail-next", {"status": 503})
        self.assertHttpError(503, "server_error", self.redeem, "TEST-OK03")
        self.backend.post("/_mock/fail-next", {"status": 429, "retry_after": 7})
        self.assertEqual(self.assertHttpError(429, "rate_limited", self.redeem, "TEST-OK03").retry_after, 7)

    def test_redirects_are_never_followed(self):
        elsewhere = MockBackend()
        self.addCleanup(elsewhere.stop)
        for status in (301, 302, 303, 307, 308):
            self.backend.state.fail_next = {"status": status, "location": elsewhere.url + "/v1/redemptions"}
            with self.assertRaises(api.Unknown, msg=status):  # the POST may already have been processed
                self.redeem("TEST-OK03")
        rid = self.redeem("TEST-PROC")["redemption_id"]
        self.backend.state.fail_next = {"status": 302, "location": elsewhere.url + "/v1/redemptions/x"}
        with self.assertRaises(api.BadResponse):
            self.api.get_redemption(rid)
        self.assertEqual(elsewhere.state.paths, [])
        self.assertEqual(elsewhere.state.authorizations, [])

    def test_refused_connection_is_not_sent(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(api.NotSent):
            api.Api(f"http://127.0.0.1:{port}", timeout=2).redeem("TEST-OK04", ACCOUNT_A, "a@example.com")

    def test_cdk_and_account_stay_out_of_urls(self):
        self.api.get_redemption(self.redeem("TEST-OK05")["redemption_id"])
        self.assertEqual(len(self.backend.state.paths), 2)
        for path in self.backend.state.paths:
            for secret in ("TEST-OK05", ACCOUNT_A, "a@example.com"):
                self.assertNotIn(secret, path)
        self.assertTrue(all(h.startswith("codex-cdk-recharge/") for h in self.backend.state.client_headers))

    def test_local_backend_bypasses_proxy(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                                          "NO_PROXY": ""}):
            self.assertEqual(api.Api(self.backend.url).redeem("TEST-OK04", ACCOUNT_B, "b@example.com")["status"],
                             "succeeded")

    def test_plain_http_only_for_local_hosts(self):
        for url in ["http://127.0.0.1:18787", "http://localhost:1", "https://pay.example.com"]:
            api.Api(url)
        for url in ["http://pay.example.com", "ftp://x", "", "pay.example.com", "https://pay.example.com:abc"]:
            with self.assertRaises(api.InsecureUrl, msg=url):
                api.Api(url)

    def test_errors_never_carry_the_cdk(self):
        for cdk in ["NOPE-SECRET-1", "TEST-EXPD"]:
            try:
                self.redeem(cdk)
            except api.ApiError as exc:
                self.assertNotIn(cdk, repr(exc) + str(exc))


class SubmitTest(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackend()
        self.api = api.Api(self.backend.url, timeout=5)

    def tearDown(self):
        self.backend.stop()

    def test_only_order_numbers_and_redemption_codes_qualify(self):
        for text, want in (("TEST-OK01", "TEST-OK01"), (" ORDER-2026 1029-0001 ", "ORDER-20261029-0001"),
                           ("abcd", "abcd"), ("A" * 64, "A" * 64)):
            self.assertEqual(api.submit_code(text), want)
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"
        for text in ("", "   ", "abc", "-ABCD", "A" * 65, "兑换码1234", "TEST_OK01", jwt,
                     '{"accessToken":"x"}', "Bearer " + jwt, "sk-proj-abcdEFGH1234", "SESS-abcdEFGH1234"):
            self.assertIsNone(api.submit_code(text), text)

    def test_submit_sends_the_checked_code_and_nothing_else(self):
        self.assertEqual(self.api.submit(" TEST-OK01 ")["status"], "received")
        self.assertEqual(self.backend.state.submissions, [{"code": "TEST-OK01"}])
        self.assertEqual(self.backend.state.authorizations, [""])
        self.assertEqual(self.backend.state.paths, ["/v1/submissions"])

    def test_submit_errors_use_the_shared_types(self):
        with self.assertRaises(api.HttpError) as ctx:
            self.api.submit("NOPE-0000")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (400, "invalid_code"))
        with self.assertRaises(api.HttpError) as ctx:
            self.api.submit("TEST-FAIL")
        self.assertEqual(ctx.exception.status, 503)
        with self.assertRaises(ValueError) as ctx:
            self.api.submit("sk-proj-abcdEFGH1234")
        self.assertNotIn("abcd", str(ctx.exception))
        self.assertEqual(len(self.backend.state.paths), 2)  # the key never left

    def test_real_backends_are_https_and_get_nothing_until_enabled(self):
        with self.assertRaises(api.InsecureUrl):
            api.Api("http://recharge.example.com")
        remote = api.Api("https://recharge.example.com")
        with mock.patch.object(remote, "_call") as call, self.assertRaises(api.NotSent) as ctx:
            remote.submit("TEST-OK01")
        self.assertEqual(ctx.exception.kind, "disabled")
        call.assert_not_called()
        with mock.patch.object(api, "SUBMIT_LIVE", True), mock.patch.object(remote, "_call") as call:
            remote.submit("TEST-OK01")
        call.assert_called_once_with("POST", "/v1/submissions", {"code": "TEST-OK01"})


if __name__ == "__main__":
    unittest.main()
