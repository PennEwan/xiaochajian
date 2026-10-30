"""Hooks with sync: the in-App notice, starting the uploader, and staying out of Codex's way."""
import ast
import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from ctr import hook, store
from ctr.sync import launcher
from ctr.sync import state as sstate

from helpers import T0
from sync_helpers import SyncCase

CTR_LIB = Path(__file__).resolve().parents[1] / "plugins" / "codex-task-recorder" / "lib" / "ctr"


class NoticeTest(SyncCase):
    def test_sync_notice_once_per_session(self):
        self.sync_on()
        self.assertEqual(self.hook("UserPromptSubmit", prompt="hi"), {"systemMessage": hook.NOTICE_SYNCING})
        self.assertIsNone(self.hook("UserPromptSubmit", prompt="again"))
        notices = [e for e in self.events() if e["kind"] == "recorder_notice"]
        self.assertEqual([n["data"]["message"] for n in notices], [hook.NOTICE_SYNCING])

    def test_sync_turned_on_mid_session_shows_the_sync_notice_once(self):
        self.assertEqual(self.hook("UserPromptSubmit", prompt="hi"), {"systemMessage": hook.NOTICE_RECORDING})
        self.sync_on()
        self.assertEqual(self.hook("UserPromptSubmit", prompt="x"), {"systemMessage": hook.NOTICE_SYNCING})
        self.assertIsNone(self.hook("UserPromptSubmit", prompt="y"))

    def test_background_sessions_get_no_notice(self):
        self.sync_on()
        self.assertIsNone(self.hook("UserPromptSubmit", transcript=False, prompt="# Overview"))

    def test_paused_sync_shows_the_local_notice(self):
        self.sync_on()
        self.sync_pause(at=10**6)
        self.assertEqual(self.hook("UserPromptSubmit", prompt="hi"), {"systemMessage": hook.NOTICE_RECORDING})


class KickTest(SyncCase):
    def test_hook_starts_the_uploader_with_the_data_dir(self):
        self.sync_on()
        self.hook("UserPromptSubmit", prompt="hi")
        self.spawn.assert_called_once()
        argv, env, cwd = self.spawn.call_args[0]
        self.assertEqual(argv[-2:], ["-m", "ctr.sync"])
        # Neither the plugin cache nor the data directory: Windows cannot delete a directory that
        # is a process's cwd, and uninstall / `ctr install` / purge remove those.
        self.assertEqual(cwd, tempfile.gettempdir())
        self.assertEqual(env["CTR_DATA_DIR"], str(self.paths.root))
        self.assertIn(str(launcher.LIB_DIR), env["PYTHONPATH"])

    def test_spawns_are_throttled(self):
        self.sync_on()
        self.hook("UserPromptSubmit", prompt="hi")
        self.hook("PreToolUse", tool_name="Bash", tool_use_id="exec-1", tool_input={})
        self.assertEqual(self.spawn.call_count, 1)

    def test_running_uploader_is_not_started_again(self):
        self.sync_on()
        fd = store.try_hold(self.sp.worker_lock)
        try:
            self.hook("UserPromptSubmit", prompt="hi")
        finally:
            store.release(fd)
        self.spawn.assert_not_called()

    def test_sync_off_never_starts_it(self):
        self.hook("UserPromptSubmit", prompt="hi")
        self.sync_on()
        self.sync_pause(at=10**6)
        self.hook("UserPromptSubmit", prompt="again")
        self.spawn.assert_not_called()

    def test_pending_revocation_starts_it(self):
        self.sync_on()
        sstate.revoke(self.sp, T0 + timedelta(days=1), delete_remote=True)
        self.hook("UserPromptSubmit", prompt="hi")
        self.spawn.assert_called_once()

    def test_spawn_failure_never_breaks_the_hook(self):
        self.sync_on()
        self.spawn.side_effect = OSError("blocked by antivirus")
        self.assertEqual(self.hook("UserPromptSubmit", prompt="hi"), {"systemMessage": hook.NOTICE_SYNCING})
        self.assertIn("user_prompt", self.kinds())

    def test_checking_a_running_uploader_is_cheap(self):
        self.sync_on()
        fd = store.try_hold(self.sp.worker_lock)
        try:
            st = sstate.load(self.sp)
            started = time.perf_counter()
            for _ in range(100):
                launcher.kick(self.paths, st)
            per_call_ms = (time.perf_counter() - started) * 1000 / 100
        finally:
            store.release(fd)
        self.assertLess(per_call_ms, 5)


class IndependenceTest(unittest.TestCase):
    def test_recorder_never_imports_the_recharge_plugin(self):
        for path in CTR_LIB.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertFalse(name.split(".")[0] == "cdkr", f"{path.name} imports {name}")

    def test_sync_does_not_read_recharge_files(self):
        for path in (CTR_LIB / "sync").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("CodexCdkRecharge", text, path.name)
            self.assertNotIn("binding.bin", text, path.name)


if __name__ == "__main__":
    unittest.main()
