import functools
import gc
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from cdkr import config, flow, paint
from cdkr.flow import Outcome, View

from cdkr_helpers import MockBackend

try:
    import tkinter
    tkinter.Tk().destroy()
    HAVE_TK = True
except Exception:  # no display / no Tk
    HAVE_TK = False


class FakeFlow:
    def __init__(self, views, outcomes=(), note=""):
        self.views, self.outcomes, self.redeemed, self.note = list(views), list(outcomes), [], note

    def load(self):
        return self.views.pop(0)

    def redeem(self, cdk, progress=None):
        self.redeemed.append(cdk)
        if progress:
            progress("处理中…")
        return self.outcomes.pop(0)

    def plan_note(self):
        return self.note


class WindowCase(unittest.TestCase):
    def setUp(self):
        # Tk objects must be freed on the main thread. Collect them here (this cleanup runs last),
        # or a later test's background thread may trigger the GC and Tcl aborts the process.
        self.addCleanup(gc.collect)

    def make(self, fake, local=True):
        from cdkr import window
        app = window.App(fake, local_backend=local, run_async=lambda fn, done: done(fn()))
        self.addCleanup(self.destroy, app)
        app.start()
        app.root.update()
        return app

    @staticmethod
    def destroy(app):
        try:
            app.root.destroy()
        except tkinter.TclError:  # the test already closed it
            pass

    @staticmethod
    def alive(app):
        try:
            return bool(app.root.winfo_exists())
        except tkinter.TclError:
            return False


@unittest.skipUnless(HAVE_TK, "needs a desktop session with Tk")
class WindowTest(WindowCase):
    def test_first_use_goes_straight_to_the_cdk(self):
        fake = FakeFlow([View(flow.READY, "CDK 将充值到上面这个 ChatGPT 账号。", "a@example.com", "Free")],
                        [Outcome(flow.SUCCEEDED, "充值成功：X。CDK TE…01 已核销。")],
                        note="Codex 账号套餐：Plus。")
        app = self.make(fake)  # no confirm-the-account step before the CDK box opens
        self.assertEqual(app.button["text"], "确认提交")
        self.assertEqual(str(app.entry["state"]), "normal")
        self.assertIn("a@example.com", app.account_var.get())
        app.cdk_var.set("TEST-OK01")
        app.button.invoke()
        self.assertEqual(fake.redeemed, ["TEST-OK01"])
        self.assertEqual(app.cdk_var.get(), "")
        self.assertIn("充值成功", app.status_var.get())
        self.assertIn("Codex 账号套餐：Plus。", app.status_var.get())  # appended after the lookup

    def test_failed_redeem_keeps_cdk_for_correction(self):
        fake = FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")],
                        [Outcome(flow.CDK_INVALID, "CDK NOPE…1234 无效，请检查是否输错。")])
        app = self.make(fake)
        app.cdk_var.set("NOPE-1234")
        app.root.update()
        app.entry.focus_force()
        app.entry.event_generate("<Return>")  # Enter in the CDK box redeems
        app.root.update()
        self.assertEqual(fake.redeemed, ["NOPE-1234"])
        self.assertEqual(app.cdk_var.get(), "NOPE-1234")
        self.assertIn("无效", app.status_var.get())

    def test_empty_cdk_cannot_be_submitted(self):
        fake = FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")])
        app = self.make(fake)
        self.assertEqual(app.button["state"], "disabled")
        app.cdk_var.set("   ")
        app.button.invoke()
        app.entry.focus_force()
        app.entry.event_generate("<Return>")
        app.root.update()
        self.assertEqual(fake.redeemed, [])
        app.cdk_var.set("TEST-OK01")
        self.assertEqual(app.button["state"], "normal")
        app.cdk_var.set("")
        self.assertEqual(app.button["state"], "disabled")

    def test_repeated_clicks_send_one_request_then_allow_retry(self):
        from cdkr import window
        fake = FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")],
                        [Outcome(flow.NOT_SENT, "连不上充值服务，请检查网络后重试。"), Outcome(flow.SUCCEEDED, "充值成功")])
        pending = []
        app = window.App(fake, local_backend=True, run_async=lambda fn, done: pending.append((fn, done)))
        self.addCleanup(self.destroy, app)
        app.start()
        pending.pop()[1](fake.load())
        app.cdk_var.set("TEST-OK01")
        for _ in range(3):
            app.button.invoke()
            app.entry.event_generate("<Return>")
        self.assertEqual(len(pending), 1)  # one request while it runs
        self.assertEqual(app.status_var.get(), "正在提交…")
        self.assertEqual(app.button["state"], "disabled")
        self.assertEqual(str(app.entry["state"]), "disabled")
        fn, done = pending.pop()
        done(fn())
        self.assertIn("重试", app.status_var.get())
        self.assertEqual(app.canvas.itemcget(app.status_text, "fill"), paint.hexc(paint.BAD_RED))
        self.assertEqual(app.button["state"], "normal")  # the CDK stays, so it can be retried
        app.button.invoke()
        fn, done = pending.pop()
        done(fn())
        self.assertEqual(fake.redeemed, ["TEST-OK01", "TEST-OK01"])
        self.assertEqual(app.status_var.get(), "提交成功。充值成功")

    def test_rejected_account_stays_on_the_cdk(self):
        fake = FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")],
                        [Outcome(flow.INVALID_ACCOUNT, "充值服务不接受这个账号信息，请联系客服。")])
        app = self.make(fake)
        app.cdk_var.set("TEST-OK01")
        app.button.invoke()
        self.assertEqual((app.button["text"], app.button["state"]), ("确认提交", "normal"))
        self.assertEqual(app.canvas.itemcget(app.status_text, "fill"), paint.hexc(paint.BAD_RED))

    def test_no_account_offers_retry(self):
        fake = FakeFlow([View(flow.NO_ACCOUNT, flow.ACCOUNT_MESSAGES[flow.NOT_LOGGED_IN]),
                         View(flow.READY, "就绪", "a@example.com", "Plus")])
        app = self.make(fake)
        self.assertEqual(app.button["text"], "重新读取")
        app.button.invoke()
        self.assertEqual(app.button["text"], "确认提交")

    def test_threaded_path_touches_tk_only_on_main_thread(self):
        import threading
        import time
        from cdkr import window
        fake = FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")],
                        [Outcome(flow.SUCCEEDED, "充值成功")])
        app = window.App(fake, local_backend=True)  # real worker threads
        self.addCleanup(app.root.destroy)
        threads, texts, say = [], [], app._say
        app._say = lambda text, tone: (threads.append(threading.current_thread()), texts.append(text),
                                       say(text, tone))
        app.root.after(100, app._drain)
        app.start()
        deadline = time.monotonic() + 5
        while str(app.entry["state"]) != "normal":  # loaded and ready for a CDK
            app.root.update()
            self.assertLess(time.monotonic(), deadline)
        app.cdk_var.set("TEST-OK01")
        app.button.invoke()
        while "充值成功" not in app.status_var.get():
            app.root.update()
            self.assertLess(time.monotonic(), deadline)
        self.assertIn("处理中…", texts)  # the worker's progress line reached the window
        self.assertTrue(all(t is threading.main_thread() for t in threads), threads)

    def test_mock_backend_is_labelled(self):
        app = self.make(FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")]), local=True)
        self.assertIn("模拟", app.footer_var.get())
        app2 = self.make(FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")]), local=False)
        self.assertNotIn("模拟", app2.footer_var.get())


@unittest.skipUnless(HAVE_TK, "needs a desktop session with Tk")
class SubmitWindowTest(WindowCase):
    """确认提交 against the local mock's /v1/submissions (CDKR_SUBMIT_MOCK=1)."""

    def setUp(self):
        super().setUp()
        self.backend = MockBackend()
        self.addCleanup(self.backend.stop)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.log = Path(home.name) / "cdkr.log"
        self.fake = FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")])
        self.pending = []

    def open(self, threaded=False):
        """The window wired as main() does with CDKR_SUBMIT_MOCK=1. Unless threaded, every request
        waits in self.pending until finish(), so the loading state can be inspected."""
        from cdkr import window
        submit = functools.partial(window.send_submission, config.Config(base_url=self.backend.url, timeout=5),
                                   self.log)
        app = window.App(self.fake, local_backend=True, submit=submit,
                         run_async=None if threaded else (lambda fn, done: self.pending.append((fn, done))))
        self.addCleanup(self.destroy, app)
        if threaded:
            app.root.after(100, app._drain)
        app.start()
        if not threaded:
            self.finish()
        return app

    def finish(self):
        fn, done = self.pending.pop(0)
        done(fn())

    def submit(self, app, text):
        app.cdk_var.set(text)
        app.button.invoke()
        self.finish()

    def status(self, app):
        return app.status_var.get(), app.canvas.itemcget(app.status_text, "fill")

    def test_loading_then_success_with_one_request_for_repeated_clicks(self):
        app = self.open()
        app.cdk_var.set("TEST-OK01")
        for _ in range(3):
            app.button.invoke()
            app.entry.event_generate("<Return>")
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(app.status_var.get(), "正在提交…")
        self.assertEqual((app.button["state"], str(app.entry["state"])), ("disabled", "disabled"))
        self.finish()
        self.assertEqual(self.status(app), ("提交成功。已收到 TE…01。", paint.hexc(paint.OK_GREEN)))
        self.assertEqual(self.backend.state.submissions, [{"code": "TEST-OK01"}])
        self.assertEqual(self.backend.state.authorizations, [""])  # no token, no login
        self.assertEqual((self.fake.redeemed, self.pending), ([], []))  # no redemption, no plan lookup
        self.assertEqual(app.cdk_var.get(), "")

    def test_failure_shows_an_error_and_allows_a_retry(self):
        app = self.open()
        self.backend.post("/_mock/fail-next", {"status": 503})
        self.submit(app, "TEST-OK02")
        self.assertEqual(self.status(app), ("提交服务暂时不可用（503），请稍后重试。", paint.hexc(paint.BAD_RED)))
        self.assertEqual((app.cdk_var.get(), app.button["state"]), ("TEST-OK02", "normal"))
        app.button.invoke()
        self.finish()
        self.assertEqual(app.status_var.get(), "提交成功。已收到 TE…02。")
        self.assertEqual(self.backend.state.paths, ["/v1/submissions"] * 2)
        self.submit(app, "NOPE-0000")
        self.assertEqual(self.status(app), ("NO…00 无效，请检查是否输错。", paint.hexc(paint.BAD_RED)))

    def test_credentials_are_refused_before_anything_is_sent(self):
        app = self.open()
        for text in ('{"accessToken": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"}', "sk-proj-abcdEFGH1234"):
            self.submit(app, text)
            self.assertIn("格式不对", app.status_var.get())
            self.assertEqual(app.cdk_var.get(), text)  # kept for correction
        self.assertEqual(self.backend.state.paths, [])

    def test_log_never_holds_what_was_typed(self):
        app = self.open()
        self.submit(app, "TEST-OK03")
        self.submit(app, "NOPE-0000")
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("submit succeeded", text)
        self.assertIn("submit cdk_invalid status=400", text)
        for typed in ("TEST-OK03", "NOPE-0000", "TE…03"):
            self.assertNotIn(typed, text)

    def test_threaded_slow_submission_is_sent_once(self):
        import time
        app = self.open(threaded=True)  # real worker threads; TEST-WAIT answers after 2 s
        self.addCleanup(lambda: [app.root.after_cancel(i)  # the _drain timer, before the window goes
                                 for i in app.root.tk.splitlist(app.root.tk.call("after", "info"))])
        deadline = time.monotonic() + 10
        while str(app.entry["state"]) != "normal":
            app.root.update()
            self.assertLess(time.monotonic(), deadline)
        app.cdk_var.set("TEST-WAIT")
        while "提交成功" not in app.status_var.get():
            app.button.invoke()  # keeps clicking while the request runs
            app.root.update()
            self.assertLess(time.monotonic(), deadline)
        self.assertEqual(self.backend.state.submissions, [{"code": "TEST-WAIT"}])


@unittest.skipUnless(HAVE_TK, "needs a desktop session with Tk")
class CardLookTest(WindowCase):
    """The 小助手-style borderless card."""

    def ready(self, **kw):
        return self.make(FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")], **kw))

    def test_is_a_borderless_card_of_the_assistant_width(self):
        app = self.ready()
        self.assertTrue(app.root.overrideredirect())
        self.assertEqual(app.root.winfo_width(), paint.CARD_W)

    def test_plan_shows_as_a_chip_next_to_the_email(self):
        app = self.ready()
        self.assertEqual(app.account_var.get(), "a@example.com")
        self.assertEqual(app.canvas.itemcget(app.plan_chip.label, "text"), "Plus")

    def test_guide_link_shows_and_opens_the_recharge_page(self):
        from cdkr import window
        app = self.ready()
        self.assertEqual(app.canvas.itemcget(app.link, "text"), window.RECHARGE_URL)
        with mock.patch("webbrowser.open") as open_:
            app._open_link()
        open_.assert_called_once_with(window.RECHARGE_URL)

    def test_close_button_closes(self):
        app = self.ready()
        app.close_button.invoke()
        self.assertFalse(self.alive(app))

    def test_escape_closes(self):
        app = self.ready()
        app.root.focus_force()
        app.root.event_generate("<Escape>")
        app.root.update()
        self.assertFalse(self.alive(app))

    def test_closing_while_redeeming_asks_first(self):
        app = self.ready()
        app._set_busy(True)
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            app.close_button.invoke()
        ask.assert_called_once()
        self.assertTrue(self.alive(app))
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            app.close_button.invoke()
        self.assertFalse(self.alive(app))

    def test_dragging_the_card_moves_the_window(self):
        app = self.ready()
        app.root.geometry("+200+150")
        app.root.update()
        press = types.SimpleNamespace(x_root=300, y_root=200)
        app._drag_start(press)
        app._drag_move(types.SimpleNamespace(x_root=340, y_root=260))
        app.root.update()
        self.assertEqual((app.root.winfo_x(), app.root.winfo_y()), (240, 210))

    def test_status_colour_follows_the_outcome(self):
        app = self.make(FakeFlow([View(flow.READY, "就绪", "a@example.com", "Plus")],
                                 [Outcome(flow.SUCCEEDED, "充值成功"), Outcome(flow.CDK_INVALID, "CDK 无效")]))
        for cdk, colour in (("TEST-OK01", paint.OK_GREEN), ("NOPE-1234", paint.BAD_RED)):
            app.cdk_var.set(cdk)
            app.button.invoke()
            self.assertEqual(app.canvas.itemcget(app.status_text, "fill"), paint.hexc(colour), cdk)

    @unittest.skipUnless(os.name == "nt", "the clip region is Win32")
    def test_clip_region_follows_the_card_height(self):
        from cdkr import window
        app = self.ready()
        self.assertEqual(window.region_height(app.root), app.root.winfo_height())
        short = app.root.winfo_height()
        app._say("很长的一段提示。" * 12, "info")   # the card grows
        app.root.update()
        self.assertGreater(app.root.winfo_height(), short + 30)
        self.assertEqual(window.region_height(app.root), app.root.winfo_height())

    def test_card_grows_with_a_long_status(self):
        app = self.ready()
        short = app.root.winfo_height()
        app._say("很长的一段提示。" * 12, "info")
        app.root.update()
        self.assertGreater(app.root.winfo_height(), short)
        footer_top = app.canvas.bbox(app.footer_text)[1]
        self.assertGreater(footer_top, app.canvas.bbox(app.status_text)[3])  # footer stays below the status


if __name__ == "__main__":
    unittest.main()
