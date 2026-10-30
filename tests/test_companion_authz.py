"""The one switch: consent turns recording + upload on together; pause / resume / revoke keep the
recorder and the sync state in step, and the uploaded data can be deleted."""
import json
import os
import unittest
from unittest import mock

from companion import authz as authz_mod
from companion.authz import OFF, ON, PAUSED, Authz, AuthzError
from ctr import state as rstate
from ctr.sync import hostgate, vault
from ctr.sync import state as sstate
from ctr.sync.config import SyncPaths

from companion_helpers import CompanionCase
from sync_helpers import MockSync


class AuthzCase(CompanionCase):
    def setUp(self):
        super().setUp()
        self.mock = MockSync()
        self.addCleanup(self.mock.stop)
        from companion import product

        self.product = product.Product(root=self.root, version="1.0.0", sync_base_url=self.mock.url)
        self.authz = Authz(self.product, self.rpaths, self.cpaths)
        self.sp = SyncPaths(self.rpaths)
        (self.home / "config.toml").write_text('[plugins."codex-companion@codex-companion"]\nenabled = true\n',
                                               encoding="utf-8")


class SwitchTest(AuthzCase):
    def test_first_enable_needs_consent(self):
        with self.assertRaises(AuthzError):
            self.authz.enable(accepted=False)
        self.assertEqual(self.authz.status().state, OFF)

    def test_enable_turns_both_on(self):
        st = self.authz.enable(accepted=True)
        self.assertEqual(st.state, ON)
        self.assertTrue(st.recording and st.uploading)
        self.assertTrue(rstate.is_recording(rstate.load(self.rpaths.state)))
        self.assertTrue(sstate.is_on(sstate.load(self.sp)))
        self.assertEqual(sstate.load(self.sp).consent_base_url, self.mock.url)
        self.spawn.assert_called()  # the uploader is started

    def test_pause_turns_both_off_and_resume_needs_no_new_consent(self):
        self.authz.enable(accepted=True)
        st = self.authz.pause()
        self.assertEqual(st.state, PAUSED)
        self.assertFalse(rstate.is_recording(rstate.load(self.rpaths.state)))
        self.assertFalse(sstate.is_on(sstate.load(self.sp)))
        st = self.authz.enable(accepted=False)
        self.assertEqual(st.state, ON)

    def test_changed_consent_text_needs_a_new_consent(self):
        self.authz.enable(accepted=True)
        self.authz.pause()
        with mock.patch.object(authz_mod, "CONSENT_VERSION", "p4-2"):
            with self.assertRaises(AuthzError):
                self.authz.enable(accepted=False)

    def test_consent_text_names_backend_and_data_dir(self):
        text = self.authz.consent_text()
        self.assertIn(self.mock.url, text)
        self.assertIn(str(self.rpaths.root), text)
        self.assertIn("随机设备 ID", text)

    def test_no_backend_configured(self):
        from companion import product

        a = Authz(product.Product(root=self.root), self.rpaths, self.cpaths)
        with self.assertRaisesRegex(AuthzError, "没有配置数据上传服务地址"):
            a.enable(accepted=True)

    def test_refused_while_the_plugin_is_disabled_in_codex(self):
        (self.home / "config.toml").write_text('[plugins."codex-companion@codex-companion"]\nenabled = false\n',
                                               encoding="utf-8")
        with self.assertRaisesRegex(AuthzError, "已在 Codex 中禁用"):
            self.authz.enable(accepted=True)

    def test_refused_while_the_plugin_is_not_installed(self):
        (self.home / "config.toml").write_text('model = "x"\n', encoding="utf-8")
        with self.assertRaisesRegex(AuthzError, "已从 Codex 中卸载"):
            self.authz.enable(accepted=True)

    def test_untrusted_hooks_do_not_prevent_consent(self):
        # Trust comes later (restart Codex, 「全部信任」); recording starts once the hooks run.
        self.assertEqual(self.authz.enable(accepted=True).state, ON)


class UrlFormTest(AuthzCase):
    def test_a_trailing_slash_in_the_build_url_does_not_break_pause_and_resume(self):
        from companion import product

        self.authz = Authz(product.Product(root=self.root, version="1.0.0", sync_base_url=self.mock.url + "/"),
                           self.rpaths, self.cpaths)
        self.authz.enable(accepted=True)
        self.assertEqual(self.authz.pause().state, PAUSED)
        self.assertEqual(self.authz.enable(accepted=False).state, ON)  # no new consent, queue kept


class RevokeTest(AuthzCase):
    def upload_something(self):
        from ctr.sync.worker import Worker

        self.authz.enable(accepted=True)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1",
                   "prompt": "hi", "cwd": "D:\\w", "transcript_path": None}
        from ctr import hook
        hook.handle(payload, self.rpaths, {}, 0.0)
        Worker(self.rpaths, gate=mock.Mock(check=mock.Mock(return_value=None))).run(once=True)
        self.assertTrue(self.mock.events())

    def test_revoke_and_delete_remote(self):
        self.upload_something()
        result = self.authz.revoke(delete_remote=True, delete_local=False)
        self.assertEqual(result, {"sent": True, "deleted": "ok", "registered": True})
        self.assertEqual(self.mock.events(), [])
        self.assertIsNone(vault.load(self.sp.device))
        self.assertEqual(self.authz.status().state, OFF)

    def test_revoke_offline_is_reported_honestly(self):
        self.upload_something()
        self.mock.stop()
        result = self.authz.revoke(delete_remote=True, delete_local=False)
        self.assertFalse(result["sent"])
        self.assertIn(sstate.OP_DELETE_REMOTE, sstate.load(self.sp).pending_ops)

    def test_revoke_can_delete_local_recordings(self):
        self.upload_something()
        self.authz.revoke(delete_remote=False, delete_local=True)
        self.assertFalse(self.rpaths.sessions.exists())

    def test_never_enabled(self):
        self.assertEqual(self.authz.revoke(False, False), {"sent": True, "deleted": None, "registered": False})


class StatusTest(AuthzCase):
    def test_host_block_is_visible(self):
        self.authz.enable(accepted=True)
        self.sp.root.mkdir(parents=True, exist_ok=True)
        self.sp.status.write_text(json.dumps({"host_blocked": hostgate.DISABLED}), encoding="utf-8")
        self.assertEqual(self.authz.status().host_blocked, hostgate.DISABLED)


if __name__ == "__main__":
    unittest.main()
