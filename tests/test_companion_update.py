"""Updates: manifest checks, the 24-hour throttle, the user's mode, and verified downloads."""
import hashlib
import os
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

from companion import local, product, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mock_update_server  # noqa: E402

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class UpdateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="companion-update-"))
        self.server = mock_update_server.make_server("127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.cpaths = local.Paths(self.tmp / "companion")
        self.product = product.Product(root=self.tmp, version="1.0.0", update_manifest_url=self.url + "/manifest.json")
        self.installer = self.tmp / "CodexCompanion-Setup-1.0.1.exe"
        self.installer.write_bytes(b"MZ" + bytes(range(256)) * 40)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def control(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with _DIRECT.open(req, timeout=5) as resp:
            return json.loads(resp.read())

    def offer(self, **kw):
        self.control("/_mock/release", {"version": "1.0.1", "path": str(self.installer), **kw})

    def requests(self):
        with _DIRECT.open(self.url + "/_mock/requests", timeout=5) as resp:
            return json.loads(resp.read())["requests"]


class ManifestTest(unittest.TestCase):
    good = {"product": "codex-companion", "latest": {"version": "1.2.0", "url": "https://u/s.exe",
                                                     "sha256": "a" * 64, "size": 10}}

    def test_parse(self):
        rel = update.parse_manifest(self.good)
        self.assertEqual((rel.version, rel.size, rel.requires_retrust), ("1.2.0", 10, False))
        self.assertIsNone(update.parse_manifest({"product": "codex-companion", "latest": None}))

    def test_rejects_bad_manifests(self):
        bad = [{"product": "other", "latest": None},
               {**self.good, "latest": {**self.good["latest"], "url": "http://evil.example/s.exe"}},
               {**self.good, "latest": {**self.good["latest"], "sha256": "xyz"}},
               {**self.good, "latest": {**self.good["latest"], "size": 0}},
               {**self.good, "latest": {**self.good["latest"], "version": "latest"}},
               {**self.good, "latest": {"version": "1.2.0"}}, "nope"]
        for doc in bad:
            with self.assertRaises(update.UpdateError, msg=doc):
                update.parse_manifest(doc)

    def test_versions(self):
        p = product.Product(root=Path("."), version="1.0.0")
        rel = update.parse_manifest(self.good)
        self.assertTrue(update.newer(p, rel))
        self.assertFalse(update.newer(product.Product(root=Path("."), version="1.2.0"), rel))
        self.assertEqual(product.version_key("0.163.0-alpha.10.2"), (0, 163, 0))


class CheckTest(UpdateCase):
    def test_finds_a_newer_release_and_identifies_only_by_version(self):
        self.offer()
        doc = update.check(self.product, self.cpaths, force=True)
        self.assertEqual(doc["latest"]["version"], "1.0.1")
        self.assertEqual(update.available(self.product, self.cpaths).version, "1.0.1")
        self.assertEqual(self.requests(), [{"path": "/manifest.json", "user_agent": "CodexCompanion/1.0.0"}])

    def test_checks_at_most_once_a_day(self):
        self.offer()
        update.check(self.product, self.cpaths, now=lambda: 1000.0)
        update.check(self.product, self.cpaths, now=lambda: 1000.0 + 3600)
        self.assertEqual(len(self.requests()), 1)
        update.check(self.product, self.cpaths, now=lambda: 1000.0 + update.CHECK_INTERVAL_S + 1)
        self.assertEqual(len(self.requests()), 2)

    def test_off_never_contacts_the_server(self):
        update.set_mode(self.cpaths, update.OFF)
        update.check(self.product, self.cpaths)
        self.assertEqual(self.requests(), [])

    def test_network_trouble_is_recorded_not_raised(self):
        self.control("/_mock/fail-next", {"status": 503})
        doc = update.check(self.product, self.cpaths, force=True)
        self.assertEqual(doc["error"], "http_503")
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(update.check(self.product, self.cpaths, force=True)["error"], "network")
        self.server = mock_update_server.make_server("127.0.0.1", 0)  # for tearDown
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def test_redirects_are_not_followed(self):
        self.control("/_mock/fail-next", {"mode": "redirect"})
        self.assertEqual(update.check(self.product, self.cpaths, force=True)["error"], "http_302")

    def test_no_release_and_same_version(self):
        update.check(self.product, self.cpaths, force=True)
        self.assertIsNone(update.available(self.product, self.cpaths))
        self.control("/_mock/release", {"version": "1.0.0", "path": str(self.installer)})
        update.check(self.product, self.cpaths, force=True)
        self.assertIsNone(update.available(self.product, self.cpaths))

    def test_notice_once_a_day(self):
        self.offer()
        update.check(self.product, self.cpaths, force=True)
        self.assertIn("1.0.1", update.notice(self.product, self.cpaths, today=date(2026, 10, 29)))
        self.assertIsNone(update.notice(self.product, self.cpaths, today=date(2026, 10, 29)))
        self.assertIsNotNone(update.notice(self.product, self.cpaths, today=date(2026, 10, 30)))


class KickTest(UpdateCase):
    def test_kick_when_due_and_throttled(self):
        with mock.patch("companion.launch.spawn_module") as spawn:
            self.assertTrue(update.kick(self.product, self.cpaths))
            self.assertFalse(update.kick(self.product, self.cpaths))  # spawn stamp: 5 minutes
        argv = spawn.call_args[0]
        self.assertEqual(argv, ("companion", ["update", "check", "--background"]))

    def test_no_kick_without_url_or_when_off_or_recent(self):
        with mock.patch("companion.launch.spawn_module") as spawn:
            self.assertFalse(update.kick(product.Product(root=self.tmp), self.cpaths))
            update.set_mode(self.cpaths, update.OFF)
            self.assertFalse(update.kick(self.product, self.cpaths))
            update.set_mode(self.cpaths, update.NOTIFY)
            local.update_json(self.cpaths.update, checked_ts=time.time())
            self.assertFalse(update.kick(self.product, self.cpaths))
        spawn.assert_not_called()

    def test_kick_never_raises(self):
        with mock.patch("companion.launch.spawn_module", side_effect=OSError):
            self.assertFalse(update.kick(self.product, self.cpaths))


class DownloadTest(UpdateCase):
    def test_verified_download(self):
        self.offer()
        update.check(self.product, self.cpaths, force=True)
        path = update.download(self.product, self.cpaths, update.available(self.product, self.cpaths))
        self.assertEqual(Path(path).read_bytes(), self.installer.read_bytes())

    def test_tampered_download_is_discarded(self):
        self.offer(tamper=True)
        update.check(self.product, self.cpaths, force=True)
        with self.assertRaises(update.UpdateError) as ctx:
            update.download(self.product, self.cpaths, update.available(self.product, self.cpaths))
        self.assertEqual(ctx.exception.kind, "hash_mismatch")
        self.assertEqual(list(self.cpaths.downloads.iterdir()), [])

    def test_size_mismatch(self):
        self.offer()
        update.check(self.product, self.cpaths, force=True)
        rel = update.available(self.product, self.cpaths)
        rel.size -= 1
        with self.assertRaises(update.UpdateError) as ctx:
            update.download(self.product, self.cpaths, rel)
        self.assertEqual(ctx.exception.kind, "size_mismatch")


class RobustnessTest(UpdateCase):
    def test_a_connection_dropped_mid_body_is_a_network_error(self):
        import http.client

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                raise http.client.IncompleteRead(b"{")

        with mock.patch("companion.update._get", return_value=Resp()):
            doc = update.check(self.product, self.cpaths, force=True)
        self.assertEqual(doc["error"], "network")
        self.assertIsNotNone(doc["checked_ts"])  # recorded: no respawn every few minutes

    def test_hooks_do_not_import_the_network_stack(self):
        import subprocess

        code = ("import io, json, sys; from companion import dispatch; "
                "raw = json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 's', 'prompt': 'hi',"
                " 'transcript_path': 'x'}).encode(); "
                "dispatch.main(stdin=io.BytesIO(raw), stdout=io.StringIO(), env={}); "
                "print(sorted(m for m in ('ssl', 'urllib.request', 'http.client') if m in sys.modules))")
        env = dict(os.environ, COMPANION_DATA_DIR=str(self.tmp / "c"), CTR_DATA_DIR=str(self.tmp / "r"))
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(proc.stdout.strip(), "[]", proc.stderr)

    def test_concurrent_state_writes(self):
        errors = []

        def writer(n):
            try:
                for i in range(30):
                    local.update_json(self.cpaths.update, **{f"k{n}": i})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual([p.name for p in self.cpaths.root.iterdir() if p.suffix == ".tmp"], [])


class BackgroundTest(UpdateCase):
    def test_kernel_is_looked_up_only_when_needed(self):
        self.offer()
        kernel = mock.Mock(return_value="0.163.0")
        with mock.patch("companion.update.install"):
            update.background_check(self.product, self.cpaths, kernel)
        kernel.assert_not_called()  # notify mode: no codex.exe started

    def test_auto_install_waits_while_the_recharge_window_is_open(self):
        update.set_mode(self.cpaths, update.AUTO)
        self.offer()
        with mock.patch("companion.update.recharge_window_open", return_value=True), \
                mock.patch("companion.update.install") as install:
            self.assertEqual(update.background_check(self.product, self.cpaths), "deferred")
        install.assert_not_called()

    def test_installer_arguments(self):
        with mock.patch("companion.launch.spawn") as spawn:
            update.install("setup.exe", silent=True, auto=True)
            update.install("setup.exe", silent=False)
        auto, manual = (c[0][0] for c in spawn.call_args_list)
        self.assertEqual(auto, ["setup.exe", "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=auto"])
        self.assertEqual(manual, ["setup.exe", "/UPDATE=1"])

    def test_notify_mode_only_records(self):
        self.offer()
        with mock.patch("companion.update.install") as install:
            self.assertEqual(update.background_check(self.product, self.cpaths), "notify")
        install.assert_not_called()

    def test_auto_mode_installs_a_verified_release(self):
        update.set_mode(self.cpaths, update.AUTO)
        self.offer()
        with mock.patch("companion.update.install") as install:
            self.assertEqual(update.background_check(self.product, self.cpaths), "installing")
        path = install.call_args[0][0]
        self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                         hashlib.sha256(self.installer.read_bytes()).hexdigest())
        self.assertTrue(install.call_args[1]["silent"])

    def test_auto_mode_waits_for_a_click_when_trust_is_needed(self):
        update.set_mode(self.cpaths, update.AUTO)
        self.offer(requires_retrust=True)
        with mock.patch("companion.update.install") as install:
            self.assertEqual(update.background_check(self.product, self.cpaths), "notify")
        install.assert_not_called()

    def test_auto_mode_respects_the_kernel_minimum(self):
        update.set_mode(self.cpaths, update.AUTO)
        self.offer(min_kernel="0.200.0")
        with mock.patch("companion.update.install") as install:
            self.assertEqual(update.background_check(self.product, self.cpaths, "0.163.0-alpha.10.2"),
                             "kernel_too_old")
        install.assert_not_called()

    def test_single_checker(self):
        from ctr import store

        self.cpaths.root.mkdir(parents=True)
        fd = store.try_hold(self.cpaths.update_lock)
        try:
            self.assertEqual(update.background_check(self.product, self.cpaths), "already_running")
        finally:
            store.release(fd)


if __name__ == "__main__":
    unittest.main()
