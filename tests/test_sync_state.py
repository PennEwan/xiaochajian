import json
import unittest
from unittest import mock
from datetime import timedelta

from ctr.sync import config, log, vault
from ctr.sync import state as sstate
from ctr.sync.config import SyncPaths

from helpers import T0, TempPathsCase, ts

URL = "https://sync.example.com"


class SyncStateTest(TempPathsCase):
    def setUp(self):
        super().setUp()
        self.sp = SyncPaths(self.paths)

    def at(self, seconds):
        return T0 + timedelta(seconds=seconds)

    def test_missing_state_is_off(self):
        self.assertIsNone(sstate.load(self.sp))
        self.assertFalse(sstate.is_on(None))
        self.assertFalse(sstate.wants_worker(None))

    def test_enable_opens_a_window_and_records_consent(self):
        st = sstate.enable(self.sp, self.at(0), URL)
        self.assertTrue(sstate.is_on(st))
        self.assertEqual(st.consent_version, sstate.CONSENT_VERSION)
        self.assertEqual(st.consent_accepted_at, ts(0))
        self.assertEqual(len(st.windows), 1)
        self.assertTrue(sstate.in_window(st, ts(5)))
        self.assertFalse(sstate.in_window(st, ts(-5)))

    def test_pause_closes_window_and_resume_opens_a_new_one(self):
        sstate.enable(self.sp, self.at(0), URL)
        st = sstate.pause(self.sp, self.at(10))
        self.assertFalse(sstate.is_on(st))
        self.assertEqual(st.stopped_reason, "paused")
        self.assertFalse(sstate.wants_worker(st))
        st = sstate.enable(self.sp, self.at(20), URL)
        self.assertEqual(st.consent_accepted_at, ts(0), "resuming keeps the original consent")
        self.assertTrue(sstate.in_window(st, ts(5)))
        self.assertFalse(sstate.in_window(st, ts(15)), "paused period is not authorised")
        self.assertTrue(sstate.in_window(st, ts(25)))

    def test_enable_twice_keeps_one_open_window(self):
        sstate.enable(self.sp, self.at(0), URL)
        st = sstate.enable(self.sp, self.at(5), URL)
        self.assertEqual(len(st.windows), 1)

    def test_revoke_forgets_consent_and_windows_and_queues_remote_ops(self):
        sstate.enable(self.sp, self.at(0), URL)
        st = sstate.revoke(self.sp, self.at(10), delete_remote=True)
        self.assertFalse(sstate.is_on(st))
        self.assertEqual(st.windows, [])
        self.assertIsNone(st.consent_version)
        self.assertEqual(st.stopped_reason, "revoked")
        self.assertEqual(st.pending_ops, ["delete_remote", "revoke_device"])
        self.assertTrue(sstate.wants_worker(st), "remote operations still need the uploader")
        self.assertFalse(sstate.in_window(st, ts(5)), "nothing from before the revocation is uploadable")

    def test_revoke_without_delete_and_completing_ops(self):
        sstate.enable(self.sp, self.at(0), URL)
        st = sstate.revoke(self.sp, self.at(10), delete_remote=False)
        self.assertEqual(st.pending_ops, ["revoke_device"])
        st = sstate.complete_op(self.sp, "revoke_device")
        self.assertEqual(st.pending_ops, [])
        self.assertFalse(sstate.wants_worker(st))

    def test_a_different_backend_needs_a_new_consent_and_starts_clean(self):
        sstate.enable(self.sp, self.at(0), URL)
        sstate.pause(self.sp, self.at(10))
        self.assertFalse(sstate.needs_new_consent(sstate.load(self.sp), URL))
        self.assertTrue(sstate.needs_new_consent(sstate.load(self.sp), "https://other.example.com"))
        st = sstate.enable(self.sp, self.at(20), "https://other.example.com")
        self.assertEqual(st.consent_base_url, "https://other.example.com")
        self.assertEqual(st.consent_accepted_at, ts(20))
        self.assertFalse(sstate.in_window(st, ts(5)), "content consented for the old backend stays local")

    def test_writers_never_blank_a_momentarily_unreadable_state(self):
        sstate.enable(self.sp, self.at(0), URL)
        sstate.revoke(self.sp, self.at(10), delete_remote=True)
        before = self.sp.state.read_text(encoding="utf-8")
        real = type(self.sp.state).read_text

        def flaky(path, *a, **kw):
            if path == self.sp.state:
                raise PermissionError("sharing violation")
            return real(path, *a, **kw)

        with mock.patch.object(type(self.sp.state), "read_text", flaky), mock.patch("time.sleep"):
            with self.assertRaises(PermissionError):
                sstate.complete_op(self.sp, "delete_remote")
        self.assertEqual(self.sp.state.read_text(encoding="utf-8"), before)
        self.assertEqual(sstate.load(self.sp).pending_ops, ["delete_remote", "revoke_device"])

    def test_corrupt_state_fails_closed(self):
        self.sp.root.mkdir(parents=True)
        self.sp.state.write_text("{not json", encoding="utf-8")
        st = sstate.load(self.sp)
        self.assertTrue(st.corrupt)
        self.assertFalse(sstate.is_on(st))
        self.assertFalse(sstate.wants_worker(st))

    def test_future_consent_version_requires_new_consent(self):
        sstate.enable(self.sp, self.at(0), URL)
        doc = json.loads(self.sp.state.read_text(encoding="utf-8"))
        doc["consent_version"] = sstate.CONSENT_VERSION + 1
        self.sp.state.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(sstate.is_on(sstate.load(self.sp)))


class SyncConfigTest(TempPathsCase):
    def test_round_trip_and_bom(self):
        sp = SyncPaths(self.paths)
        self.assertIsNone(config.load(sp).base_url)
        config.save(sp, config.Config(base_url="https://sync.example.com"))
        self.assertEqual(config.load(sp).base_url, "https://sync.example.com")
        sp.config.write_bytes(b"\xef\xbb\xbf" + json.dumps({"base_url": "http://127.0.0.1:1"}).encode())
        self.assertEqual(config.load(sp).base_url, "http://127.0.0.1:1")


class VaultTest(TempPathsCase):
    def test_round_trip_and_tamper(self):
        path = self.tmp / "device.bin"
        vault.save(path, {"device_id": "dev_1", "device_token": "ctrd_secret"})
        self.assertNotIn(b"ctrd_secret", path.read_bytes())
        self.assertEqual(vault.load(path)["device_token"], "ctrd_secret")
        path.write_bytes(path.read_bytes()[:-3] + b"xyz")
        self.assertIsNone(vault.load(path))
        vault.clear(path)
        self.assertIsNone(vault.load(path))


class SyncLogTest(TempPathsCase):
    def test_only_fixed_identifiers(self):
        path = self.tmp / "sync.log"
        log.write(path, "upload", "ok", status=200, n=5, ms=12)
        self.assertRegex(path.read_text(encoding="utf-8"), r"upload ok status=200 n=5 ms=12\n$")
        with self.assertRaises(ValueError):
            log.write(path, "upload", "prompt text with spaces")


if __name__ == "__main__":
    unittest.main()
