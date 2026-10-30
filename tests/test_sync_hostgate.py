"""The uploader stops once Codex no longer runs the host plugin (disabled / uninstalled / a hook
switched off or untrusted), and does not start in that state."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ctr import timeutil
from ctr.sync import hostgate, launcher
from ctr.sync import state as sstate

from test_sync_worker import FakeTime, WorkerCase

REPO = Path(__file__).resolve().parents[1]
RECORDER = REPO / "plugins" / "codex-task-recorder"
PID = "codex-companion@codex-companion"


def plugin_root(base: Path, plugin_id: str | None = PID) -> Path:
    root = base / "plugin"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "hooks").mkdir()
    shutil.copy(RECORDER / ".codex-plugin" / "plugin.json", root / ".codex-plugin" / "plugin.json")
    shutil.copy(RECORDER / "hooks" / "hooks.json", root / "hooks" / "hooks.json")
    if plugin_id:
        (root / "product.json").write_text(json.dumps({"plugin_id": plugin_id}), encoding="utf-8")
    return root


def config_text(keys, enabled=True, trusted=True, off=()):
    lines = ['model = "gpt-x"', "", f'[plugins."{PID}"]', f"enabled = {'true' if enabled else 'false'}", ""]
    for key in keys:
        lines.append(f'[hooks.state."{key}"]')
        if trusted:
            lines.append('trusted_hash = "sha256:00"')
        if key in off:
            lines.append("enabled = false")
        lines.append("")
    return "\n".join(lines)


class GateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctr-gate-"))
        self.root = plugin_root(self.tmp)
        self.home = self.tmp / "codex-home"
        self.home.mkdir()
        self.keys = hostgate.hook_keys(self.root, PID)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        cfg = self.home / "config.toml"
        cfg.write_text(text, encoding="utf-8")
        st = cfg.stat()  # make every rewrite visible to the mtime cache, even within one tick
        os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000 * (1 + len(text) % 7)))

    def gate(self):
        return hostgate.Gate(self.root, self.home)


class IdentityTest(GateCase):
    def test_product_json_names_the_host_plugin(self):
        self.assertEqual(hostgate.host_plugin_id(self.root), PID)

    def test_inferred_from_the_codex_plugin_cache_path(self):
        cached = self.tmp / ".codex" / "plugins" / "cache" / "ctr-local" / "codex-task-recorder" / "0.1.0"
        cached.mkdir(parents=True)
        self.assertEqual(hostgate.host_plugin_id(cached), "codex-task-recorder@ctr-local")

    def test_a_source_checkout_is_not_gated(self):
        self.assertIsNone(hostgate.host_plugin_id(RECORDER))
        self.assertIsNone(hostgate.Gate(RECORDER, self.home).check())

    def test_hook_keys_follow_codex_naming(self):
        self.assertEqual(len(self.keys), 12)
        self.assertIn(f"{PID}:hooks/hooks.json:user_prompt_submit:0:0", self.keys)
        self.assertIn(f"{PID}:hooks/hooks.json:pre_tool_use:0:0", self.keys)
        self.assertIn(f"{PID}:hooks/hooks.json:session_start:0:0", self.keys)


class VerdictTest(GateCase):
    def test_enabled_and_trusted_is_allowed(self):
        self.write(config_text(self.keys))
        self.assertIsNone(self.gate().check())

    def test_plugin_disabled(self):
        self.write(config_text(self.keys, enabled=False))
        self.assertEqual(self.gate().check(), hostgate.DISABLED)

    def test_plugin_uninstalled(self):
        self.write('model = "gpt-x"\n')
        self.assertEqual(self.gate().check(), hostgate.UNINSTALLED)

    def test_no_config_file_means_uninstalled(self):
        self.assertEqual(self.gate().check(), hostgate.UNINSTALLED)

    def test_one_hook_switched_off(self):
        self.write(config_text(self.keys, off=(self.keys[3],)))
        self.assertEqual(self.gate().check(), hostgate.HOOK_OFF)

    def test_trust_missing(self):
        self.write(config_text(self.keys, trusted=False))
        self.assertEqual(self.gate().check(), hostgate.UNTRUSTED)
        self.write(config_text(self.keys[1:]))  # one hook never trusted
        self.assertEqual(self.gate().check(), hostgate.UNTRUSTED)

    def test_unreadable_config_fails_closed(self):
        self.write("this is [not toml")
        with mock.patch("ctr.sync.hostgate.time.sleep"):
            self.assertEqual(self.gate().check(), hostgate.UNREADABLE)

    def test_reread_only_when_the_file_changes(self):
        self.write(config_text(self.keys))
        gate = self.gate()
        self.assertIsNone(gate.check())
        with mock.patch("ctr.sync.hostgate.tomllib.load", side_effect=AssertionError("re-read")):
            self.assertIsNone(gate.check())
        self.write(config_text(self.keys, enabled=False))
        self.assertEqual(gate.check(), hostgate.DISABLED)

    def test_codex_home_follows_the_environment(self):
        self.assertEqual(hostgate.codex_home({"CODEX_HOME": r"D:\x"}), Path(r"D:\x"))
        self.assertEqual(hostgate.codex_home({}), Path.home() / ".codex")


class FakeGate:
    def __init__(self, reason=None):
        self.reason = reason
        self.calls = 0

    def check(self):
        self.calls += 1
        return self.reason


class WorkerGateTest(WorkerCase):
    def test_blocked_host_uploads_nothing_and_exits(self):
        self.record_turn(1)
        gate = FakeGate(hostgate.DISABLED)
        self.assertEqual(self.worker(gate=gate).run(), "host_blocked")
        self.assertEqual(self.mock.events(), [])
        self.assertEqual(self.status()["host_blocked"], hostgate.DISABLED)

    def test_uploads_again_once_the_plugin_is_back(self):
        self.record_turn(1)
        self.worker(gate=FakeGate(hostgate.UNTRUSTED)).run()
        self.assertEqual(self.worker(gate=FakeGate(None)).run(once=True), "idle")
        self.assertEqual(self.remote_ids(), self.local_ids())
        self.assertIsNone(self.status()["host_blocked"])

    def test_disabling_during_a_backoff_ends_the_wait(self):
        self.record_turn(1)
        self.mock.fail_next(status=503, count=50)
        gate = FakeGate(None)
        time_ = FakeTime()

        def sleep(seconds):
            time_.sleep(seconds)
            gate.reason = hostgate.DISABLED  # the user disables the plugin while we wait

        worker = self.worker(gate=gate, clock=time_.clock, sleep=sleep)
        self.assertEqual(worker.run(), "disabled")
        self.assertLessEqual(sum(time_.sleeps), 2.0)

    def test_pending_revocation_still_reaches_the_backend(self):
        self.record_turn(1)
        self.worker().run(once=True)
        sstate.revoke(self.sp, timeutil.now(), delete_remote=True)
        self.assertEqual(self.worker(gate=FakeGate(hostgate.UNINSTALLED)).run(once=True), "disabled")
        self.assertEqual(self.mock.events(), [])  # remote data deleted
        self.assertEqual(sstate.load(self.sp).pending_ops, [])


class LauncherGateTest(WorkerCase):
    def test_no_spawn_while_blocked(self):
        with mock.patch("ctr.sync.gate.check", return_value=hostgate.DISABLED):
            self.assertFalse(launcher.kick(self.paths, force=True))
        self.spawn.assert_not_called()

    def test_spawn_for_a_pending_revocation_even_when_blocked(self):
        sstate.revoke(self.sp, timeutil.now(), delete_remote=False)
        with mock.patch("ctr.sync.gate.check", return_value=hostgate.UNINSTALLED):
            self.assertTrue(launcher.kick(self.paths, force=True))
        self.spawn.assert_called_once()

    def test_check_is_cheap(self):
        tmp = Path(tempfile.mkdtemp(prefix="ctr-gate-"))
        try:
            root = plugin_root(tmp)
            keys = hostgate.hook_keys(root, PID)
            (tmp / "config.toml").write_text(config_text(keys) + "\n".join(
                f'[projects.\'d:\\\\p{i}\']\ntrust_level = "trusted"\n' for i in range(300)), encoding="utf-8")
            started = time.perf_counter()
            self.assertIsNone(hostgate.Gate(root, tmp).check())
            self.assertLess(time.perf_counter() - started, 0.05)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
