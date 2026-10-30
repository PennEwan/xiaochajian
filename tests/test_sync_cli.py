import io
import os
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ctr import cli, state, store
from ctr.sync import worker
from ctr.sync import config as sconfig
from ctr.sync import state as sstate
from ctr.sync import vault
from ctr.sync.worker import Worker

from sync_helpers import MockSync, SyncCase


class SyncCliCase(SyncCase):
    def setUp(self):
        super().setUp()
        self.mock = MockSync()
        env = mock.patch.dict(os.environ, {"CTR_DATA_DIR": str(self.paths.root)})
        env.start()
        self.addCleanup(env.stop)
        trust = mock.patch.object(cli, "_print_hook_trust")
        trust.start()
        self.addCleanup(trust.stop)

    def tearDown(self):
        self.mock.stop()
        super().tearDown()

    def ctr(self, *argv, answers=()):
        out = io.StringIO()
        replies = iter(answers)

        def fake_input(prompt=""):
            out.write(prompt)  # like input(): the question shows up on stdout
            return next(replies, "n")

        with redirect_stdout(out), mock.patch("builtins.input", fake_input):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def enable_sync(self):
        code, out = self.ctr("sync", "enable", "--i-consent", "--skip-install", "--base-url", self.mock.url)
        self.assertEqual(code, 0, out)
        return out

    def upload(self):
        self.hook("UserPromptSubmit", prompt="hello")
        self.assertEqual(Worker(self.paths, sleep=lambda s: None).run(once=True), "idle")


class EnableTest(SyncCliCase):
    def test_requires_a_backend_address(self):
        code, out = self.ctr("sync", "enable", "--i-consent", "--skip-install")
        self.assertEqual(code, 1)
        self.assertIn("尚未配置同步服务地址", out)
        self.assertIsNone(sstate.load(self.sp))

    def test_rejects_insecure_addresses(self):
        code, out = self.ctr("sync", "enable", "--i-consent", "--skip-install", "--base-url", "http://sync.example.com")
        self.assertEqual(code, 1)
        self.assertIn("https://", out)

    def test_consent_text_then_on_and_uploader_started(self):
        out = self.enable_sync()
        self.assertIn("数据同步授权", out)
        self.assertIn(self.mock.url, out)
        self.assertIn("CDK", out)
        self.assertTrue(sstate.is_on(sstate.load(self.sp)))
        self.spawn.assert_called_once()

    def test_declining_leaves_sync_off(self):
        code, out = self.ctr("sync", "enable", "--skip-install", "--base-url", self.mock.url, answers=["n"])
        self.assertEqual(code, 1)
        self.assertFalse(sstate.is_on(sstate.load(self.sp)))
        self.spawn.assert_not_called()

    def test_without_recording_consent_both_consents_are_asked(self):
        self.paths.state.unlink()
        code, out = self.ctr("sync", "enable", "--skip-install", "--base-url", self.mock.url, answers=["y", "y"])
        self.assertEqual(code, 0, out)
        self.assertIn("记录同意说明", out)
        self.assertIn("数据同步授权", out)
        self.assertTrue(state.is_recording(state.load(self.paths.state)))
        self.assertTrue(sstate.is_on(sstate.load(self.sp)))

    def test_resume_after_pause_is_a_one_line_confirmation(self):
        self.enable_sync()
        accepted = sstate.load(self.sp).consent_accepted_at
        self.ctr("sync", "pause")
        code, out = self.ctr("sync", "enable", "--skip-install", answers=["y"])
        self.assertEqual(code, 0, out)
        self.assertIn("将恢复数据同步", out)
        self.assertNotIn("数据同步授权", out)
        st = sstate.load(self.sp)
        self.assertTrue(sstate.is_on(st))
        self.assertEqual(st.consent_accepted_at, accepted)
        self.assertEqual(len(st.windows), 2)


class BackendTest(SyncCliCase):
    def test_declined_consent_does_not_keep_the_backend(self):
        self.ctr("sync", "enable", "--skip-install", "--base-url", self.mock.url, answers=["n"])
        self.assertIsNone(sconfig.load(self.sp).base_url)

    def test_switching_backend_is_refused_while_a_device_is_registered(self):
        self.enable_sync()
        self.upload()
        for argv in (("sync", "config", "--base-url", "https://other.example.com"),
                     ("sync", "enable", "--i-consent", "--skip-install", "--base-url", "https://other.example.com")):
            code, out = self.ctr(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("撤销同步", out)
        self.assertEqual(sconfig.load(self.sp).base_url, self.mock.url)
        self.assertTrue(sstate.is_on(sstate.load(self.sp)))

    def test_new_backend_pauses_sync_until_the_user_agrees_again(self):
        self.enable_sync()
        code, out = self.ctr("sync", "config", "--base-url", "http://localhost:9")
        self.assertIn("重新同意", out)
        self.assertFalse(sstate.is_on(sstate.load(self.sp)))
        code, out = self.ctr("sync", "enable", "--skip-install", answers=["y"])
        self.assertIn("数据同步授权", out, "full consent text, naming the new backend")
        self.assertIn("http://localhost:9", out)
        st = sstate.load(self.sp)
        self.assertEqual(st.consent_base_url, "http://localhost:9")
        self.assertEqual(len(st.windows), 1, "nothing consented for the old backend carries over")


class StatusTest(SyncCliCase):
    def test_pause_and_status(self):
        self.enable_sync()
        code, out = self.ctr("sync", "pause")
        self.assertIn("已暂停数据同步", out)
        code, out = self.ctr("sync", "status", "--offline")
        self.assertIn("○ 已暂停", out)

    def test_status_after_upload_shows_device_and_backend_count(self):
        self.enable_sync()
        self.upload()
        code, out = self.ctr("sync", "status")
        dev = vault.load(self.sp.device)
        self.assertIn(dev["device_id"], out)
        self.assertIn("待上传：0 条", out)
        self.assertRegex(out, r"后台已收到：\d+ 条")
        self.assertNotIn(dev["device_token"], out)

    def test_record_status_includes_a_sync_line(self):
        self.enable_sync()
        code, out = self.ctr("status")
        self.assertIn("数据同步：● 同步中", out)


class RevokeTest(SyncCliCase):
    def test_revoke_and_delete_remote_data(self):
        self.enable_sync()
        self.upload()
        self.assertTrue(self.mock.events())
        code, out = self.ctr("sync", "revoke", "--yes", "--delete-remote")
        self.assertEqual(code, 0, out)
        self.assertIn("后台已删除", out)
        self.assertEqual(self.mock.events(), [])
        self.assertEqual(self.mock.state.devices, {})
        st = sstate.load(self.sp)
        self.assertEqual((st.pending_ops, st.windows, st.consent_version), ([], [], None))

    def test_revoke_interactively_keeping_remote_data(self):
        self.enable_sync()
        self.upload()
        code, out = self.ctr("sync", "revoke", answers=["y", "n"])
        self.assertEqual(code, 0, out)
        self.assertTrue(self.mock.events())
        self.assertEqual(self.mock.state.devices, {})

    def test_offline_revoke_is_queued_for_the_uploader(self):
        self.enable_sync()
        self.upload()
        dev = vault.load(self.sp.device)
        vault.save(self.sp.device, {**dev, "base_url": "http://127.0.0.1:9"})
        self.spawn.reset_mock()
        code, out = self.ctr("sync", "revoke", "--yes", "--delete-remote")
        self.assertIn("暂未送达", out)
        self.assertEqual(sstate.load(self.sp).pending_ops, ["delete_remote", "revoke_device"])
        self.spawn.assert_called_once()
        self.assertFalse(sstate.is_on(sstate.load(self.sp)))

    def test_revoke_waits_for_a_busy_uploader_and_leaves_the_ops_to_it(self):
        self.enable_sync()
        self.upload()
        fd = store.try_hold(self.sp.worker_lock)  # an uploader in the middle of a request
        try:
            with mock.patch.object(worker, "OPS_LOCK_WAIT_S", 0.3):
                code, out = self.ctr("sync", "revoke", "--yes", "--delete-remote")
        finally:
            store.release(fd)
        self.assertIn("上传进程", out)
        self.assertNotIn("后台已删除", out)
        self.assertEqual(sstate.load(self.sp).pending_ops, ["delete_remote", "revoke_device"])
        self.assertTrue(self.mock.events(), "nothing was sent while the uploader held the lock")

    def test_revoke_reports_ops_that_the_uploader_ran_while_the_cli_waited(self):
        # Seen live: the uploader held the lock, ran the queued delete + revoke itself, then exited;
        # the CLI got the lock with nothing left to do and must still report what happened.
        self.enable_sync()
        self.upload()
        fd = store.try_hold(self.sp.worker_lock)

        def uploader():
            Worker(self.paths).run_ops()
            store.release(fd)

        threading.Timer(0.5, uploader).start()
        with mock.patch.object(worker, "OPS_LOCK_WAIT_S", 10):
            code, out = self.ctr("sync", "revoke", "--yes", "--delete-remote")
        self.assertIn("后台已删除", out)
        self.assertNotIn("未能删除", out)
        self.assertEqual(self.mock.events(), [])

    def test_revoke_reports_a_delete_the_backend_refused(self):
        self.enable_sync()
        self.upload()
        self.mock.fail_next(status=404, path="/v1/devices/me/data")
        code, out = self.ctr("sync", "revoke", "--yes", "--delete-remote")
        self.assertIn("未能删除", out)
        self.assertNotIn("后台已删除", out)
        self.assertEqual(sstate.load(self.sp).pending_ops, [])

    def test_uninstall_can_revoke_and_purge(self):
        self.enable_sync()
        self.upload()
        with mock.patch.object(cli, "uninstall_plugin", return_value=True):
            code, out = self.ctr("uninstall", "--purge", "--revoke-sync")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mock.events(), [])
        self.assertFalse(self.paths.root.exists())


if __name__ == "__main__":
    unittest.main()
