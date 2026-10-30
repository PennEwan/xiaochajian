"""The single hook entry: /充值 and /记录 are blocked and never recorded; everything else goes to
the recorder with the bundle's wording; update notices and checks never break a hook."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from companion import dispatch, local, product, update
from ctr import state as rstate
from ctr.paths import Paths as RecorderPaths
from ctr.sync import config as sconfig
from ctr.sync import state as sstate
from ctr.sync.config import SyncPaths

from helpers import T0

REPO = Path(__file__).resolve().parents[1]
SID = "01a1634e-e806-7252-8f31-c4c0d9bf63c2"


class DispatchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="companion-dispatch-"))
        self.rpaths = RecorderPaths(self.tmp / "recorder")
        self.cpaths = local.Paths(self.tmp / "companion")
        self.product = product.Product(root=self.tmp / "plugin", version="1.0.0",
                                       update_manifest_url="http://127.0.0.1:9/manifest.json")
        self.recharge, self.panel = mock.Mock(return_value=True), mock.Mock(return_value=True)
        kick = mock.patch("companion.update.kick")
        self.kick = kick.start()
        self.addCleanup(kick.stop)
        spawn = mock.patch("ctr.sync.launcher.spawn")
        spawn.start()
        self.addCleanup(spawn.stop)
        self.rollout = self.tmp / "rollout.jsonl"
        self.rollout.write_text("", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hook(self, event="UserPromptSubmit", **fields):
        payload = {"hook_event_name": event, "session_id": SID, "cwd": "D:\\w", "model": "gpt-a",
                   "turn_id": "t1", "transcript_path": str(self.rollout), **fields}
        raw = json.dumps(payload).encode("utf-8")
        return dispatch.handle(raw, product=self.product, cpaths=self.cpaths, rpaths=self.rpaths, env={},
                               started=0.0, open_recharge=self.recharge, open_panel=self.panel)

    def consent(self):
        rstate.enable(self.rpaths.state, T0 - timedelta(hours=1))
        sp = SyncPaths(self.rpaths)
        sconfig.save(sp, sconfig.Config(base_url="http://127.0.0.1:9"))
        sstate.enable(sp, T0 - timedelta(hours=1), "http://127.0.0.1:9")

    def recorded(self):
        files = list(self.rpaths.sessions.glob("*/*.jsonl")) if self.rpaths.sessions.exists() else []
        return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


class CommandTest(DispatchCase):
    def test_recharge_command_blocks_and_is_never_recorded(self):
        self.consent()
        out = self.run_hook(prompt="/充值 ABCD-1234-EFGH-5678")
        self.assertEqual(out["decision"], "block")
        self.recharge.assert_called_once()
        self.assertEqual(self.recorded(), [])
        self.assertNotIn("ABCD", json.dumps(out, ensure_ascii=False))

    def test_full_width_commands(self):
        self.assertEqual(self.run_hook(prompt="／充值")["decision"], "block")
        self.assertEqual(self.run_hook(prompt="  ／记录")["decision"], "block")

    def test_panel_command_opens_the_panel(self):
        self.consent()
        out = self.run_hook(prompt="/记录")
        self.assertEqual(out, {"decision": "block", "reason": dispatch.REASON_PANEL})
        self.panel.assert_called_once()
        self.assertEqual(self.recorded(), [])

    def test_panel_that_fails_to_open_still_blocks(self):
        self.panel.side_effect = OSError
        self.assertEqual(self.run_hook(prompt="/记录")["reason"], dispatch.REASON_PANEL_FAILED)

    def test_recharge_window_that_fails_points_to_the_start_menu(self):
        self.recharge.return_value = False
        reason = self.run_hook(prompt="/充值")["reason"]
        self.assertEqual(reason, dispatch.REASON_RECHARGE_FAILED)
        self.assertNotIn(".cmd", reason)

    def test_any_failure_still_keeps_commands_from_the_model(self):
        out = io.StringIO()
        raw = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "/充值 ABCD-1234"}).encode("utf-8")
        with mock.patch("companion.dispatch.handle", side_effect=ImportError("no cdkr")):
            code = dispatch.main(stdin=io.BytesIO(raw), stdout=out, env={}, rpaths=self.rpaths, cpaths=self.cpaths)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"decision": "block", "reason": dispatch.REASON_BROKEN})
        out = io.StringIO()
        raw = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hello"}).encode("utf-8")
        with mock.patch("companion.dispatch.handle", side_effect=ImportError("no cdkr")):
            dispatch.main(stdin=io.BytesIO(raw), stdout=out, env={}, rpaths=self.rpaths, cpaths=self.cpaths)
        self.assertEqual(out.getvalue(), "")  # ordinary prompts pass untouched

    def test_commands_only_at_the_start(self):
        self.consent()
        self.assertNotEqual((self.run_hook(prompt="请解释 /充值 命令") or {}).get("decision"), "block")
        self.recharge.assert_not_called()


class RecordingTest(DispatchCase):
    def test_not_consented_records_nothing_and_says_nothing(self):
        self.assertIsNone(self.run_hook(prompt="hello"))
        self.assertEqual(self.recorded(), [])

    def test_consented_records_with_the_bundle_notice(self):
        self.consent()
        out = self.run_hook(prompt="hello")
        self.assertEqual(out, {"systemMessage": dispatch.NOTICES["syncing"]})
        kinds = [e["kind"] for e in self.recorded()]
        self.assertIn("user_prompt", kinds)
        self.assertIsNone(self.run_hook(prompt="again"))  # once per session

    def test_other_events_go_to_the_recorder(self):
        self.consent()
        self.run_hook("PreToolUse", tool_name="Bash", tool_use_id="exec-1", tool_input={"command": "dir"})
        self.assertIn("tool_call", [e["kind"] for e in self.recorded()])

    def test_a_recorder_crash_does_not_break_the_hook(self):
        self.consent()
        with mock.patch("ctr.hook.handle", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.run_hook(prompt="hello"))
        self.assertIn("hook runtimeerror", self.cpaths.log.read_text(encoding="utf-8"))


class UpdateNoticeTest(DispatchCase):
    def offer(self, version="1.0.1", **kw):
        local.write_json(self.cpaths.update, {"checked_ts": 1, "latest": {
            "version": version, "url": "https://x/s.exe", "sha256": "0" * 64, "size": 10, **kw}})

    def test_available_update_mentioned_once_a_day(self):
        self.offer()
        out = self.run_hook(prompt="hello")
        self.assertIn("有新版本 1.0.1", out["systemMessage"])
        self.assertIsNone(self.run_hook(prompt="again"))

    def test_appended_to_the_recording_notice(self):
        self.consent()
        self.offer()
        lines = self.run_hook(prompt="hello")["systemMessage"].splitlines()
        self.assertEqual(lines[0], dispatch.NOTICES["syncing"])
        self.assertIn("1.0.1", lines[1])

    def test_not_when_off_or_not_newer(self):
        update.set_mode(self.cpaths, update.OFF)
        self.offer()
        self.assertIsNone(self.run_hook(prompt="hello"))
        update.set_mode(self.cpaths, update.NOTIFY)
        self.offer(version="1.0.0")
        self.assertIsNone(self.run_hook(prompt="hello"))

    def test_background_sessions_get_nothing_and_trigger_no_check(self):
        self.offer()
        self.assertIsNone(self.run_hook(prompt="# Overview", transcript_path=None))
        self.kick.assert_not_called()

    def test_a_failing_update_notice_keeps_the_recording_notice(self):
        self.consent()
        with mock.patch("companion.update.notice", side_effect=FileNotFoundError):
            self.assertEqual(self.run_hook(prompt="hello"), {"systemMessage": dispatch.NOTICES["syncing"]})

    def test_check_is_kicked_on_user_prompts(self):
        self.run_hook(prompt="hello")
        self.kick.assert_called_once()


class EntryScriptTest(unittest.TestCase):
    """hooks/hook.py as Codex runs it: a separate process reading stdin."""

    def run_entry(self, payload: dict, lib_ok: bool = True) -> tuple[int, str]:
        tmp = Path(tempfile.mkdtemp(prefix="companion-entry-"))
        env = dict(os.environ, COMPANION_DATA_DIR=str(tmp / "c"), CTR_DATA_DIR=str(tmp / "r"),
                   PYTHONPATH=os.pathsep.join(str(REPO / "plugins" / p / "lib")
                                              for p in ("codex-task-recorder", "codex-cdk-recharge")))
        script = REPO / "plugins" / "codex-companion" / "hooks" / "hook.py"
        if not lib_ok:  # a copy of the entry without its lib directory
            (tmp / "p" / "hooks").mkdir(parents=True)
            (tmp / "p" / "hooks" / "hook.py").write_bytes(script.read_bytes())
            script, env["PYTHONPATH"] = tmp / "p" / "hooks" / "hook.py", ""
        proc = subprocess.run([sys.executable, str(script)], input=json.dumps(payload).encode("utf-8"),
                              capture_output=True, env=env, timeout=60)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        return proc.returncode, proc.stdout.decode("utf-8")

    def test_ordinary_prompt_prints_nothing(self):
        code, out = self.run_entry({"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "hi"})
        self.assertEqual((code, out), (0, ""))

    def test_broken_install_still_blocks_commands(self):
        for prompt in ("/充值 X", "/记录"):
            code, out = self.run_entry({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, lib_ok=False)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["decision"], "block")
        code, out = self.run_entry({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}, lib_ok=False)
        self.assertEqual((code, out), (0, ""))

    def test_garbage_input(self):
        tmp = Path(tempfile.mkdtemp(prefix="companion-entry-"))
        out = io.StringIO()
        code = dispatch.main(stdin=io.BytesIO(b"\xff not json"), stdout=out, env={},
                             rpaths=RecorderPaths(tmp / "r"), cpaths=local.Paths(tmp / "c"))
        self.assertEqual((code, out.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
