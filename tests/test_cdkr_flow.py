import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path

from cdkr import config, flow
from cdkr.flow import Account, AccountError

from cdkr_helpers import MockBackend

ACCOUNT = Account("acct-aaaa", "alice@example.com", "free")
OTHER = Account("acct-bbbb", "bob@example.com", "plus")


class FakeCodex:
    """Stands in for `account/read`; `after` is what Codex reports once a recharge went through."""

    def __init__(self, account=ACCOUNT, after=None, error=None):
        self.account, self.after, self.error, self.reads = account, after, error, 0

    def __call__(self):
        self.reads += 1
        if self.error:
            raise AccountError(self.error)
        if self.after and self.reads > 1:
            return self.after
        return self.account


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cdkr-flow-"))
        self.paths = config.Paths(self.tmp)
        self.backend = MockBackend()
        self.codex = FakeCodex()

    def tearDown(self):
        self.backend.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, base_url=None, timeout=5.0, codex=None, **kw):
        cfg = config.Config(base_url=base_url or self.backend.url, timeout=timeout)
        kw.setdefault("sleep", lambda s: None)
        return flow.Recharge(self.paths, cfg, codex or self.codex, **kw)

    def ready(self, **kw):
        r = self.make(**kw)
        self.assertEqual(r.load().state, flow.READY)
        return r


class AccountTest(FlowCase):
    def test_codex_account_is_the_target_without_binding(self):
        r = self.make()
        view = r.load()
        self.assertEqual((view.state, view.email, view.plan), (flow.READY, "alice@example.com", "Free"))
        self.assertIn("上面这个 ChatGPT 账号", view.message)
        self.assertEqual(r.redeem("TEST-OK01").kind, flow.SUCCEEDED)
        self.assertIn(("TEST-OK01", ACCOUNT.account_id), self.backend.state.by_key)
        self.assertEqual(self.backend.state.paths, ["/v1/redemptions"])  # no /v1/bindings first
        self.assertEqual(self.backend.state.authorizations, [""])        # and no token of any kind
        self.assertEqual([f.name for f in self.tmp.iterdir()], ["cdkr.log"])  # nothing else kept

    def test_another_codex_account_is_ready_at_once(self):
        self.ready().redeem("TEST-OK01")
        r = self.make(codex=FakeCodex(OTHER))
        view = r.load()
        self.assertEqual((view.state, view.email), (flow.READY, "bob@example.com"))
        self.assertEqual(r.redeem("TEST-OK02").kind, flow.SUCCEEDED)
        self.assertIn(("TEST-OK02", OTHER.account_id), self.backend.state.by_key)

    def test_unconfigured_backend_disables_recharge_without_reading_account(self):
        cfg = config.Config()
        self.assertIsNone(cfg.base_url)
        codex = FakeCodex()
        r = flow.Recharge(self.paths, cfg, codex)
        view = r.load()
        self.assertEqual(view.state, flow.NOT_CONFIGURED)
        self.assertIn("cdkr.cmd config", view.message)
        self.assertEqual(codex.reads, 0)
        self.assertEqual(r.redeem("TEST-OK01").kind, flow.NOT_READY)

    def test_codex_account_problems_disable_recharge(self):
        for kind in (flow.KERNEL_MISSING, flow.NOT_LOGGED_IN, flow.NOT_CHATGPT, flow.READ_FAILED):
            view = self.make(codex=FakeCodex(error=kind)).load()
            self.assertEqual(view.state, flow.NO_ACCOUNT, kind)
            self.assertEqual(view.message, flow.ACCOUNT_MESSAGES[kind])


class RedeemTest(FlowCase):
    def test_valid_cdk_succeeds_and_message_masks_it(self):
        out = self.ready().redeem("TEST-OK01")
        self.assertEqual(out.kind, flow.SUCCEEDED)
        self.assertIn("ChatGPT Plus", out.message)
        self.assertIn("TE…01", out.message)
        self.assertNotIn("TEST-OK01", out.message)

    def test_second_submission_reports_duplicate(self):
        r = self.ready()
        r.redeem("TEST-OK01")
        out = r.redeem("TEST-OK01")
        self.assertEqual(out.kind, flow.REPLAYED)
        self.assertIn("未重复扣除", out.message)

    def test_whitespace_is_ignored(self):
        self.assertEqual(self.ready().redeem("  TEST-OK02 \n").kind, flow.SUCCEEDED)

    def test_malformed_cdk_is_rejected_without_a_request(self):
        r = self.ready()
        sent = len(self.backend.state.paths)
        for bad in ["", "   ", "abc", "中文卡密1234", "x" * 129]:
            self.assertEqual(r.redeem(bad).kind, flow.INVALID_FORMAT, bad)
        self.assertEqual(len(self.backend.state.paths), sent)

    def test_backend_rejections(self):
        r = self.ready()
        self.assertEqual(r.redeem("NOPE-1234").kind, flow.CDK_INVALID)
        self.assertEqual(r.redeem("TEST-EXPD").kind, flow.CDK_EXPIRED)
        failed = r.redeem("TEST-FAIL")
        self.assertEqual(failed.kind, flow.FULFILLMENT_FAILED)
        self.assertIn("上游开通失败", failed.message)
        self.assertIn("未被扣除", failed.message)

    def test_cdk_used_by_someone_else(self):
        self.ready().redeem("TEST-OK03")
        other = self.ready(codex=FakeCodex(OTHER))
        self.assertEqual(other.redeem("TEST-OK03").kind, flow.CDK_USED)

    def test_processing_is_polled_until_done(self):
        out = self.ready(poll_interval=0).redeem("TEST-PROC")
        self.assertEqual(out.kind, flow.SUCCEEDED)

    def test_timeout_then_retry_confirms_success_once(self):
        self.backend.state.slow_seconds = 0.8
        r = self.ready(timeout=0.3, sleep=time.sleep, retry_delays=(1.0,))
        progress = []
        out = r.redeem("TEST-SLOW", progress=progress.append)
        self.assertEqual(out.kind, flow.SUCCEEDED)
        self.assertTrue(any("重试" in p for p in progress))
        self.assertEqual(self.backend.state.consumed("TEST-SLOW"), 1)

    def test_backend_unreachable_from_the_start_is_not_sent(self):
        r = self.ready()
        r.config = config.Config(base_url=f"http://127.0.0.1:{closed_port()}", timeout=2)
        out = r.redeem("TEST-OK04")
        self.assertEqual(out.kind, flow.NOT_SENT)
        self.assertIn("未使用", out.message)

    def test_no_answer_after_retries_is_unknown(self):
        r = self.ready(retry_delays=(0, 0))
        with silent_server() as url:
            r.config = config.Config(base_url=url, timeout=0.3)
            out = r.redeem("TEST-OK04")
        self.assertEqual(out.kind, flow.UNKNOWN)
        self.assertIn("不会重复扣除", out.message)

    def test_not_sent_after_an_unanswered_attempt_is_still_unknown(self):
        # The first request reached a server that never answered; then the server vanished.
        # The first one may have been processed, so "CDK 未使用" would be false.
        server = silent_server()
        url = server.__enter__()
        self.addCleanup(server.__exit__)
        r = self.ready(retry_delays=(0,), sleep=lambda s: server.sock.close())
        r.config = config.Config(base_url=url, timeout=0.3)
        out = r.redeem("TEST-OK04")
        self.assertEqual(out.kind, flow.UNKNOWN)
        self.assertNotIn("未使用", out.message)

    def test_server_error_after_an_unanswered_attempt_is_unknown(self):
        self.backend.state.slow_seconds = 0.6
        r = self.ready(timeout=0.3, retry_delays=(0,),
                       sleep=lambda s: self.backend.post("/_mock/fail-next", {"status": 502}))
        self.assertEqual(r.redeem("TEST-SLOW").kind, flow.UNKNOWN)

    def test_processing_past_the_poll_window(self):
        out = self.ready(poll_interval=0, poll_timeout=0).redeem("TEST-PROC")
        self.assertEqual(out.kind, flow.PROCESSING)
        self.assertIn("不会重复扣除", out.message)

    def test_processing_then_failed(self):
        out = self.ready(poll_interval=0).redeem("TEST-PFAL")
        self.assertEqual(out.kind, flow.FULFILLMENT_FAILED)
        self.assertIn("未被扣除", out.message)

    def test_account_the_backend_rejects(self):
        r = self.ready()
        r.account = Account("acct-cccc", "", None)
        out = r.redeem("TEST-OK04")
        self.assertEqual(out.kind, flow.INVALID_ACCOUNT)
        self.assertIn("不接受这个账号信息", out.message)

    def test_server_error_and_rate_limit(self):
        r = self.ready()
        self.backend.post("/_mock/fail-next", {"status": 502})
        self.assertEqual(r.redeem("TEST-OK04").kind, flow.SERVER_ERROR)
        self.backend.post("/_mock/fail-next", {"status": 429, "retry_after": 30})
        out = r.redeem("TEST-OK04")
        self.assertEqual(out.kind, flow.RATE_LIMITED)
        self.assertIn("30", out.message)

    def test_success_is_reported_before_the_plan_is_looked_up(self):
        codex = FakeCodex(ACCOUNT, after=Account(ACCOUNT.account_id, ACCOUNT.email, "plus"))
        r = self.ready(codex=codex)
        out = r.redeem("TEST-OK05")
        self.assertEqual((out.kind, codex.reads), (flow.SUCCEEDED, 1))
        self.assertNotIn("Codex 账号套餐", out.message)
        self.assertEqual(r.plan_note(), "Codex 账号套餐：Plus。")

    def test_unchanged_plan_says_it_may_update_later(self):
        r = self.ready()
        r.redeem("TEST-OK05")
        self.assertIn("重启 Codex", r.plan_note())

    def test_plan_lookup_failure_is_silent(self):
        class Flaky(FakeCodex):
            def __call__(self):
                self.reads += 1
                if self.reads > 1:
                    raise OSError("kernel directory changed during an App update")
                return self.account
        r = self.ready(codex=Flaky())
        self.assertEqual(r.redeem("TEST-OK05").kind, flow.SUCCEEDED)
        self.assertEqual(r.plan_note(), "")

    def test_backend_supplied_error_codes_are_not_logged_verbatim(self):
        r = self.ready()
        self.backend.state.fail_next = {"status": 500}
        self.backend.state.error_code_override = "secretcdkvalue"
        r.redeem("TEST-OK04")
        self.assertNotIn("secretcdkvalue", self.paths.log.read_text(encoding="utf-8"))

    def test_no_redemption_before_the_account_is_read(self):
        self.assertEqual(self.make().redeem("TEST-OK05").kind, flow.NOT_READY)
        r = self.make(codex=FakeCodex(error=flow.NOT_LOGGED_IN))
        r.load()
        self.assertEqual(r.redeem("TEST-OK05").kind, flow.NOT_READY)
        self.assertEqual(self.backend.state.paths, [])

    def test_log_never_contains_cdk_account_or_email(self):
        r = self.ready()
        for cdk in ["TEST-OK05", "NOPE-SECRET-77", "TEST-FAIL"]:
            r.redeem(cdk)
        log = self.paths.log.read_text(encoding="utf-8")
        self.assertIn("redeem", log)
        for secret in ["TEST-OK05", "NOPE-SECRET-77", "TEST-FAIL", "alice@example.com", "acct-aaaa"]:
            self.assertNotIn(secret, log)


class MaskTest(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(flow.mask("ABCD-EFGH-IJKL"), "ABCD…IJKL")  # 8 of 14 shown
        self.assertEqual(flow.mask("TEST-OK01"), "TE…01")         # 4 of 9
        self.assertEqual(flow.mask("ABCDEF"), "AB…")


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class silent_server:
    """Accepts TCP connections and never answers: the request is sent, the reply never comes."""

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def __exit__(self, *exc):
        self.sock.close()


if __name__ == "__main__":
    unittest.main()
