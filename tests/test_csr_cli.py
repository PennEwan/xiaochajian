import io
import json
import re
import unittest
from unittest import mock

from csr import cli

from csr_helpers import SID, SID2, Rollout, TempDirCase
from test_csr_scanner import three_prompts

CONTENT = ("任务一", "任务二", "回复一", "回复三", "a.txt", "No such file", "检查目录结构")


class CliCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.codex_home = self.tmp / "codex-home"
        self.env = {"LOCALAPPDATA": str(self.tmp / "appdata"), "CODEX_HOME": str(self.codex_home)}

    def run_cli(self, *argv, stdin: str = "", db=True):
        out, err = io.StringIO(), io.StringIO()
        args = list(argv) + (["--db", str(self.db_path)] if db else [])
        code = cli.main(args, out=out, err=err, env=self.env, stdin=io.StringIO(stdin))
        return code, out.getvalue(), err.getvalue()

    def assert_no_content(self, text: str):
        for word in CONTENT:
            self.assertNotIn(word, text)


class ScanStatsTest(CliCase):
    def setUp(self):
        super().setUp()
        self.write(three_prompts().data())

    def test_scan_then_stats_show_counts_without_content(self):
        code, out, err = self.run_cli("scan", "--dir", str(self.sessions))
        self.assertEqual((code, err), (0, ""))
        self.assertIn(SID, out)
        self.assert_no_content(out)

        code, out, _ = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn(f"当前会话：{SID}", out)
        for label in ("用户任务", "模型", "模型回复", "工具调用", "解析失败", "缺失"):
            self.assertIn(label, out)
        self.assertIn("gpt-6-luna", out)
        self.assertIn("gpt-6.1-sol", out)
        self.assert_no_content(out)

    def test_stats_json(self):
        self.run_cli("scan", "--dir", str(self.sessions))
        code, out, _ = self.run_cli("stats", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["current_session"], SID)
        [s] = data["sessions"]
        self.assertEqual((s["prompts"], s["replies"]["total"], s["tool_calls"]["model_level"]), (3, 4, 2))
        self.assert_no_content(out)

    def test_coverage_markdown(self):
        self.run_cli("scan", "--dir", str(self.sessions))
        code, out, _ = self.run_cli("coverage", "--markdown")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("| 字段 |"))

    def test_default_database_lives_under_localappdata(self):
        code, _, _ = self.run_cli("scan", "--dir", str(self.sessions), db=False)
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "appdata" / "CodexSessionReader" / "csr.sqlite3").exists())


class SafetyTest(CliCase):
    def test_missing_directory_is_refused(self):
        code, _, err = self.run_cli("scan", "--dir", str(self.tmp / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("不存在", err)

    def test_database_inside_scanned_directory_is_refused(self):
        self.db_path = self.sessions / "csr.sqlite3"
        code, _, err = self.run_cli("scan", "--dir", str(self.sessions))
        self.assertEqual(code, 2)
        self.assertFalse(self.db_path.exists())
        self.assertIn("不能", err)

    def test_database_inside_codex_home_is_refused(self):
        self.db_path = self.codex_home / "x" / "csr.sqlite3"
        code, _, _ = self.run_cli("scan", "--dir", str(self.sessions))
        self.assertEqual(code, 2)
        self.assertFalse(self.db_path.exists())

    def test_commands_other_than_scan_need_an_existing_database(self):
        code, _, err = self.run_cli("stats")
        self.assertEqual(code, 2)
        self.assertIn("scan", err)
        self.assertFalse(self.db_path.exists())


class ExportPurgeTest(CliCase):
    def setUp(self):
        super().setUp()
        self.write(three_prompts().data(), SID)
        self.write(Rollout(SID2).meta().simple_turn("x", "另一个", "会话").data(), SID2)
        self.run_cli("scan", "--dir", str(self.sessions))
        self.export = self.tmp / "exports" / "events.jsonl"

    def test_export_writes_normalised_events(self):
        code, out, _ = self.run_cli("export", "--out", str(self.export), "--session", SID)
        self.assertEqual(code, 0)
        lines = self.export.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        self.assertTrue(events)
        self.assertEqual({e["session_id"] for e in events}, {SID})
        self.assertEqual({e["schema"] for e in events}, {"csr.event/v1"})
        self.assertIn(str(len(events)), out)

    def test_export_inside_scanned_or_codex_directory_is_refused(self):
        for target in (self.sessions / "x.jsonl", self.codex_home / "x.jsonl"):
            code, _, _ = self.run_cli("export", "--out", str(target))
            self.assertEqual(code, 2)
            self.assertFalse(target.exists())

    def test_purge_all_needs_confirmation(self):
        self.run_cli("export", "--out", str(self.export))
        code, _, _ = self.run_cli("purge", "--all", stdin="n\n")
        self.assertEqual(code, 1)
        self.assertTrue(self.db_path.exists())
        self.assertTrue(self.export.exists())

    def test_purge_all_removes_database_and_exports(self):
        self.run_cli("export", "--out", str(self.export))
        code, out, _ = self.run_cli("purge", "--all", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(self.export.exists())
        leftovers = [p for p in self.db_path.parent.glob("csr.sqlite3*")]
        self.assertEqual(leftovers, [])
        self.assertTrue(all(p.exists() for p in self.sessions.iterdir()))  # sources untouched

    def test_purge_one_session_by_unique_prefix(self):
        code, _, _ = self.run_cli("purge", "--session", SID[:13], "--yes")
        self.assertEqual(code, 0)
        _, out, _ = self.run_cli("stats", "--json")
        self.assertEqual([s["session_id"] for s in json.loads(out)["sessions"]], [SID2])

    def test_purge_refuses_unknown_or_ambiguous_session(self):
        for prefix in ("ffff", "01a18"):
            code, _, err = self.run_cli("purge", "--session", prefix, "--yes")
            self.assertEqual(code, 2, prefix)
            self.assertTrue(err)
        _, out, _ = self.run_cli("stats", "--json")
        self.assertEqual(len(json.loads(out)["sessions"]), 2)

    def test_purge_all_keeps_a_user_chosen_folder(self):
        code, _, _ = self.run_cli("purge", "--all", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(self.db_path.parent.exists())  # "out" is not our default folder


class WatchTest(CliCase):
    def test_watch_picks_up_appended_records(self):
        data = three_prompts().data()
        half = data.index(b"\n", len(data) // 2) + 1
        self.write(data[:half])

        def append_rest(_seconds):
            self.append(data[half:])

        with mock.patch.object(cli.time, "sleep", side_effect=append_rest):
            code, out, _ = self.run_cli("watch", "--dir", str(self.sessions), "--interval", "1",
                                        "--iterations", "2")
        self.assertEqual(code, 0)
        self.assertEqual(len(re.findall(r"\+\d+ 事件", out)), 2)  # one line per scan that found something
        self.assertIn("Prompt 3", out)
        self.assert_no_content(out)

    def test_ctrl_c_stops_cleanly(self):
        self.write(three_prompts().data())
        with mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            code, out, _ = self.run_cli("watch", "--dir", str(self.sessions))
        self.assertEqual(code, 0)
        self.assertIn("已停止", out)


if __name__ == "__main__":
    unittest.main()
