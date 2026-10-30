"""Tests for plugins/codex-native-assist (bin/assist.exe, built by native/assist/build.cmd).

The exe is driven the way Codex and users drive it: MCP over stdio, `collect-once` for one collector
pass, `pause`, and the test-only `flow-test` entry (ASSIST_TEST_MODE=1) for the recharge flow against
the in-process mock backend. The shared store is read back with Python's own sqlite3, an independent
SQLite implementation. Every test uses its own data dir and fake CODEX_HOME.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path

from cdkr_helpers import MockBackend
import threading
import urllib.request

import mock_sync_server  # scripts/ (on sys.path via cdkr_helpers)
from native_rollout import TOOL_KINDS, TURN_KINDS, Rollout, iso, name_for, utc_now

REPO = Path(__file__).resolve().parents[1]
EXE = REPO / "plugins" / "codex-native-assist" / "bin" / "assist.exe"
ACCOUNT = {"id": "acct-0001", "email": "user@example.com", "plan": "plus"}
SID, SID2 = "01a20000-0000-7000-8000-000000000001", "01a20000-0000-7000-8000-000000000002"


@unittest.skipUnless(EXE.exists(), "build native/assist/build.cmd first")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nat-test-"))
        self.data, self.home = self.tmp / "data", self.tmp / "codex-home"
        self.data.mkdir()
        self.day = self.home / "sessions" / "2026" / "10" / "29"
        self.day.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ASSIST_")}
        # Release behaviour (recording off until the user consents); the debug default has its own test.
        self.env.update(ASSIST_DATA_DIR=str(self.data), CODEX_HOME=str(self.home), ASSIST_HEARTBEAT_SEC="1",
                        ASSIST_COLLECT_MS="300", ASSIST_RECORDING_DEFAULT="off")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_exe(self, *args: str, stdin: str = "", env: dict | None = None) -> dict:
        proc = subprocess.run([str(EXE), *args], input=stdin.encode("utf-8"), capture_output=True,
                              env=env or self.env, timeout=180)
        return json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])

    def lifecycle(self) -> list[dict]:
        path = self.data / "lifecycle.jsonl"
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        complete = text[:text.rfind("\n") + 1]  # servers may be appending right now: skip a half-written line
        return [json.loads(x) for x in complete.splitlines() if x.strip()]

    # ---- the shared store, read and seeded through Python's sqlite3 ----------------------------

    def sql(self, query: str, args: tuple = ()) -> list[tuple]:
        if not (self.data / "assist.sqlite3").exists():
            self.run_exe("status")  # creates the schema
        conn = sqlite3.connect(self.data / "assist.sqlite3", timeout=10)
        try:
            rows = conn.execute(query, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def consent(self, periods, enabled=True, scope="all", folders=()):
        """Throwaway store only: stands in for the user ticking the box in the consent window."""
        self.sql("DELETE FROM consent_periods")
        for a, b in periods:
            self.sql("INSERT INTO consent_periods(started_at, ended_at) VALUES (?,?)", (iso(a), iso(b) if b else None))
        self.sql("UPDATE consent SET enabled = ?, scope = ?, folders = ?, version = version + 1 WHERE id = 1",
                 (1 if enabled else 0, scope, json.dumps(list(folders))))

    def events(self) -> list[dict]:
        return [json.loads(b) for (b,) in self.sql("SELECT body FROM events ORDER BY session_id, ordinal, idx")]

    def kinds(self) -> Counter:
        return Counter(e["kind"] for e in self.events())

    def blob(self) -> str:
        return json.dumps(self.events(), ensure_ascii=False)

    def issues(self) -> list[tuple]:
        return self.sql("SELECT category, code, detail, offset, count FROM issues ORDER BY id")


class McpProtocolTest(Base):
    def start(self, extra_env: dict | None = None):
        return subprocess.Popen([str(EXE), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                env={**self.env, **(extra_env or {})})

    def rpc(self, p, rid, method, params=None, raw: bytes | None = None):
        p.stdin.write(raw or (json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n").encode())
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    def close(self, *procs):
        for p in procs:
            if p.poll() is None:
                p.stdin.close()
                p.wait(10)
            p.stdout.close()

    def test_handshake_tools_and_errors(self):
        p = self.start()
        try:
            init = self.rpc(p, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "t", "version": "1"}})["result"]
            self.assertEqual(init["protocolVersion"], "2025-06-18")
            self.assertIn("CDK", init["instructions"])
            tools = {t["name"]: t for t in self.rpc(p, 2, "tools/list")["result"]["tools"]}
            self.assertEqual(set(tools), {"open_recharge_window", "open_recording_settings", "recording_status",
                                          "pause_recording", "plugin_diagnostics", "recharge_launcher"})
            # The CDK is never a tool parameter: it only exists inside the window process.
            self.assertEqual(set(tools["open_recharge_window"]["inputSchema"]["properties"]), {"wait"})
            for t in tools.values():
                self.assertNotIn("cdk", json.dumps(t["inputSchema"]).lower())
            # Turning recording on has no tool: only the consent window can do it.
            self.assertNotIn("enable_recording", tools)
            self.assertTrue(tools["recording_status"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["open_recharge_window"]["annotations"]["openWorldHint"])
            self.assertEqual(self.rpc(p, 3, "ping")["result"], {})
            self.assertEqual(self.rpc(p, 4, "no/such")["error"]["code"], -32601)
            self.assertEqual(self.rpc(p, None, "", raw=b"{not json\n")["error"]["code"], -32700)
            self.assertEqual(self.rpc(p, 5, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"], "2025-06-18")
        finally:
            self.close(p)
        self.assertEqual(p.returncode, 0)
        self.assertEqual([e for e in self.lifecycle() if e["event"] == "exit"][-1]["reason"], "stdin_eof")

    def test_app_entry_launcher_and_resource(self):
        # The fixed sidebar entry: an app-only tool (hidden from the model) whose ui:// page opens the window.
        p = self.start()
        try:
            init = self.rpc(p, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
            self.assertIn("resources", init["capabilities"])
            tools = {t["name"]: t for t in self.rpc(p, 2, "tools/list")["result"]["tools"]}
            meta = tools["recharge_launcher"]["_meta"]
            uri = meta["ui"]["resourceUri"]
            self.assertEqual(meta["ui"]["visibility"], ["app"])
            self.assertEqual(meta["openai/outputTemplate"], uri)
            entry = meta["openai/ui"]["entrypoints"][0]
            self.assertEqual(entry["type"], "global")
            self.assertEqual(entry["quickAction"]["target"], {"type": "tool", "name": "open_recharge_window",
                                                              "arguments": {"wait": False}})
            self.assertEqual(self.rpc(p, 3, "resources/list")["result"]["resources"][0]["uri"], uri)
            content = self.rpc(p, 4, "resources/read", {"uri": uri})["result"]["contents"][0]
            self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
            self.assertIn("open_recharge_window", content["text"])
            self.assertNotIn("<input", content["text"])  # the CDK is typed only in the native window
            self.assertEqual(self.rpc(p, 5, "resources/read", {"uri": "ui://nope"})["error"]["code"], -32002)
            res = self.rpc(p, 6, "tools/call", {"name": "recharge_launcher", "arguments": {}})["result"]
            self.assertFalse(res["isError"])
        finally:
            self.close(p)

    def status(self, extra_env: dict | None = None) -> dict:
        p = self.start(extra_env)
        try:
            self.rpc(p, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
            return self.rpc(p, 2, "tools/call", {"name": "recording_status", "arguments": {}})["result"]
        finally:
            self.close(p)

    def test_debug_default_turns_recording_on_once(self):
        # Debug builds (plugin-config.json "debug_recording_default_on"): recording turns itself on once per
        # store, for all sessions; once the user turns it off it stays off.
        on = {"ASSIST_RECORDING_DEFAULT": "on"}
        st = self.status(on)
        self.assertTrue(st["structuredContent"]["enabled"])
        self.assertEqual((st["structuredContent"]["changed_by"], st["structuredContent"]["scope"]), ("debug_default", "all"))
        self.assertIn("调试版默认开启", st["content"][0]["text"])
        self.assertEqual(len(st["structuredContent"]["periods"]), 1)
        subprocess.run([str(EXE), "pause"], env={**self.env, **on}, capture_output=True, timeout=60)
        st = self.status(on)
        self.assertFalse(st["structuredContent"]["enabled"])
        self.assertEqual(st["structuredContent"]["changed_by"], "cli")
        # Without the debug setting a fresh store stays off.
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        st = self.status({"ASSIST_DATA_DIR": str(fresh)})
        self.assertFalse(st["structuredContent"]["enabled"])
        self.assertEqual(st["structuredContent"]["periods"], [])

    def test_data_dir_is_codex_plugin_data_not_appdata(self):
        # The App is an MSIX package: AppData writes from its children are redirected into the package.
        # Inside Codex's plugin cache the exe uses Codex's own plugins\data\<plugin>-<marketplace>.
        cached = self.home / "plugins" / "cache" / "mk" / "codex-native-assist" / "0.2.0" / "bin" / "assist.exe"
        cached.parent.mkdir(parents=True)
        shutil.copy(EXE, cached)
        env = {k: v for k, v in self.env.items() if k != "ASSIST_DATA_DIR"}
        out = json.loads(subprocess.run([str(cached), "status"], capture_output=True, env=env, timeout=60).stdout)
        self.assertEqual(Path(out["data_dir"]), self.home / "plugins" / "data" / "codex-native-assist-mk")
        self.assertTrue((self.home / "plugins" / "data" / "codex-native-assist-mk" / "assist.sqlite3").exists())

    def test_launcher_runs_a_copy_so_the_plugin_folder_stays_deletable(self):
        # Codex App: uninstalling a plugin whose exe is running fails and leaves a half-deleted folder.
        root = self.home / "plugins" / "cache" / "mk" / "codex-native-assist" / "0.2.5"
        shutil.copytree(REPO / "plugins" / "codex-native-assist", root)
        env = {k: v for k, v in self.env.items() if k != "ASSIST_DATA_DIR"}
        p = subprocess.Popen(["cmd.exe", "/d", "/c", "bin\\launch.cmd", "mcp"], cwd=root, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, env=env)
        try:
            p.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n')
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline())["result"]["serverInfo"]["name"], "codex-native-assist")
            data = self.home / "plugins" / "data" / "codex-native-assist-mk"
            start = [json.loads(x) for x in (data / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
                     if '"start"' in x][-1]
            self.assertTrue(Path(start["exe"]).is_relative_to(data / "bin"), start["exe"])
            self.assertEqual(start["parent"], "cmd.exe")
            shutil.rmtree(root)  # what Codex's uninstall does — must succeed while the server runs
            self.assertFalse(root.exists())
            p.stdin.write(b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline())["result"], {})
        finally:
            p.stdin.close()
            p.wait(10)
            p.stdout.close()

    def test_status_and_pause_tools(self):
        self.consent([(utc_now() - timedelta(minutes=1), None)])
        p = self.start()
        try:
            self.rpc(p, 1, "initialize", {"protocolVersion": "2025-06-18"})
            res = self.rpc(p, 2, "tools/call", {"name": "recording_status", "arguments": {}})["result"]
            self.assertTrue(res["structuredContent"]["enabled"])
            res = self.rpc(p, 3, "tools/call", {"name": "pause_recording", "arguments": {}})["result"]
            self.assertFalse(res["isError"])
            self.assertFalse(res["structuredContent"]["enabled"])
            self.assertIn("已关闭", res["content"][0]["text"])
        finally:
            self.close(p)
        (ended,), = self.sql("SELECT ended_at FROM consent_periods")
        self.assertIsNotNone(ended)

    def test_window_tools_refuse_when_plugin_disabled(self):
        (self.home / "config.toml").write_text('[plugins."codex-native-assist@ctr-local"]\nenabled = false\n', encoding="utf-8")
        p = self.start()
        try:
            self.rpc(p, 1, "initialize", {"protocolVersion": "2025-06-18"})
            for rid, tool in ((2, "open_recharge_window"), (3, "open_recording_settings")):
                res = self.rpc(p, rid, "tools/call", {"name": tool, "arguments": {}})["result"]
                self.assertTrue(res["isError"])
                self.assertIn("禁用", res["content"][0]["text"])
            self.assertFalse(self.rpc(p, 4, "tools/call", {"name": "pause_recording", "arguments": {}})["result"]["isError"])
        finally:
            self.close(p)

    def test_one_collector_leader_and_takeover(self):
        a, b = self.start(), self.start()
        try:
            def leaders():
                return sorted({e["pid"] for e in self.lifecycle() if e["event"] == "collector_leader"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not leaders():
                time.sleep(0.2)
            time.sleep(1)
            first = leaders()
            self.assertEqual(len(first), 1, first)
            leader, other = (a, b) if a.pid == first[0] else (b, a)
            leader.kill()  # Codex terminates servers without notice
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and len(leaders()) < 2:
                time.sleep(0.3)
            self.assertEqual(leaders(), sorted([leader.pid, other.pid]))
        finally:
            self.close(a, b)


class CollectorTest(Base):
    def setUp(self):
        super().setUp()
        self.now = utc_now()

    def write(self, r: Rollout, sid: str = SID) -> Path:
        path = self.day / name_for(sid)
        path.write_text(r.text(), encoding="utf-8")
        return path

    def collect(self) -> dict:
        return self.run_exe("collect-once")

    def test_off_collects_nothing(self):
        self.write(Rollout(SID, self.now).meta().turn("t1", "hello", "hi"))
        self.assertEqual(self.collect()["state"], "off")
        self.assertEqual(self.events(), [])

    def test_each_output_and_tool_call_counted_once(self):
        t = self.now - timedelta(minutes=10)
        self.consent([(t, None)])
        self.write(Rollout(SID, t + timedelta(seconds=1)).meta().turn("t1", "list files", "done", command="dir")
                   .turn("t2", "again", "done again"))
        self.collect()
        self.collect()  # a second pass adds nothing
        expected = Counter({"session_meta": 1})
        for _ in range(2):
            expected.update(TURN_KINDS)
        expected.update(TOOL_KINDS)
        self.assertEqual(self.kinds(), expected)
        keys = [k for (k,) in self.sql("SELECT event_key FROM events")]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(self.issues(), [])
        (dups,), = self.sql("SELECT COUNT(*) FROM skipped WHERE category LIKE 'duplicate:%'")
        self.assertGreater(dups, 0)  # the second copies were seen and counted, not stored
        text = self.blob()
        for unwanted in ("DEV INSTRUCTIONS", "SYSTEM PROMPT TEXT", "acct-SECRET", "user-SECRET", "gAAAAENCRYPTED", "used_percent"):
            self.assertNotIn(unwanted, text)
        prompt = next(e for e in self.events() if e["kind"] == "user_prompt")
        self.assertEqual((prompt["data"]["text"], prompt["turn_id"], prompt["model"]), ("list files", "t1", "gpt-6-luna"))

    def test_only_consented_periods_and_redaction(self):
        t = self.now - timedelta(hours=1)
        self.consent([(t + timedelta(minutes=10), t + timedelta(minutes=20)), (t + timedelta(minutes=30), None)])
        r = Rollout(SID, t).meta()
        r.at(t + timedelta(minutes=5)).turn("a", "before consent", "x")
        r.at(t + timedelta(minutes=12)).turn("b", "inside sk-proj-" + "Q" * 30 + " 兑换码：AB12CD34EF56", "y")
        r.at(t + timedelta(minutes=25)).turn("c", "during pause", "z")
        r.at(t + timedelta(minutes=35)).turn("d", "after resume", "w")
        self.write(r)
        self.collect()
        prompts = [e["data"]["text"] for e in self.events() if e["kind"] == "user_prompt"]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(prompts[0].startswith("inside [REDACTED:openai_key]"))
        self.assertIn("[REDACTED:cdk]", prompts[0])
        self.assertEqual(prompts[1], "after resume")
        self.assertNotIn("AB12CD34EF56", self.blob())

    def test_incremental_partial_line_and_handle_held_open(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        r = Rollout(SID, t + timedelta(seconds=1)).meta().turn("t1", "one", "r1")
        path = self.day / name_for(SID)
        f = open(path, "w", encoding="utf-8")  # Codex keeps the file open while the session is loaded
        try:
            f.write(r.text())
            f.flush()
            self.collect()
            more = Rollout(SID, t + timedelta(minutes=1))
            more.ordinal = r.ordinal
            more.turn("t2", "two", "r2")
            text = more.text()
            f.write(text[:40])  # mid-write
            f.flush()
            self.collect()
            self.assertEqual([e["data"]["text"] for e in self.events() if e["kind"] == "user_prompt"], ["one"])
            f.write(text[40:])
            f.flush()
            self.collect()
            self.collect()
        finally:
            f.close()
        self.assertEqual([e["data"]["text"] for e in self.events() if e["kind"] == "user_prompt"], ["one", "two"])
        self.assertEqual(self.issues(), [])  # no false "missing", "parse" or "order" reports either

    def test_scope_isolates_sessions_by_folder(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)], scope="folders", folders=["D:\\work"])
        self.write(Rollout(SID, t + timedelta(seconds=1), cwd="D:\\work\\proj").meta().turn("t1", "in scope", "ok"))
        self.write(Rollout(SID2, t + timedelta(seconds=1), cwd="D:\\private").meta().turn("t1", "out of scope", "ok"), SID2)
        self.collect()
        self.assertIn("in scope", self.blob())
        self.assertNotIn("out of scope", self.blob())
        self.assertEqual({e["session_id"] for e in self.events()}, {SID})

    def test_large_history_before_consent_is_skipped_by_seeking(self):
        old = self.now - timedelta(days=2)
        r = Rollout(SID, old, step_ms=1).meta()
        for i in range(3000):
            r.turn(f"old{i}", "old history " + "x" * 50, "old reply")
        self.consent([(self.now - timedelta(minutes=1), None)])
        r.at(self.now).turn("fresh", "fresh prompt", "fresh reply")
        self.write(r)
        status = self.collect()
        self.assertIn("fresh prompt", self.blob())
        self.assertNotIn("old history", self.blob())
        self.assertLess(status["lines_read"], 2000, status)  # of ~45,000 lines
        self.assertEqual([i for i in self.issues() if i[1] == "ordinal_gap"], [])  # the jump is not a gap

    def test_rename_continues_without_duplicates(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        r = Rollout(SID, t + timedelta(seconds=1)).meta().turn("t1", "before rename", "ok")
        path = self.write(r)
        self.collect()
        moved = self.day.parent / "30" / path.name
        moved.parent.mkdir()
        path.rename(moved)
        r.at(self.now).turn("t2", "after rename", "ok")
        moved.write_text(r.text(), encoding="utf-8")  # same file id? write_text truncates in place, keeping it
        self.collect()
        prompts = [e["data"]["text"] for e in self.events() if e["kind"] == "user_prompt"]
        self.assertEqual(prompts, ["before rename", "after rename"])
        self.assertIn("renamed", [i[1] for i in self.issues()])

    def test_rewritten_or_recreated_file_does_not_duplicate(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        r = Rollout(SID, t + timedelta(seconds=1)).meta().turn("t1", "one", "ok")
        path = self.write(r)
        self.collect()
        path.unlink()  # e.g. restored from an archive: new file id, same content and more
        r.at(self.now).turn("t2", "two", "ok")
        self.write(r)
        self.collect()
        self.assertEqual([e["data"]["text"] for e in self.events() if e["kind"] == "user_prompt"], ["one", "two"])
        keys = [k for (k,) in self.sql("SELECT event_key FROM events")]
        self.assertEqual(len(keys), len(set(keys)))

    def zst(self, original: str, content_size: int, mtime: float | None = None) -> Path:
        archive = self.home / "archived_sessions"
        archive.mkdir(exist_ok=True)
        path = archive / (original + ".zst")
        # zstd frame header: magic, FHD 0xA0 (single segment, 4-byte content size), size LE, then junk
        path.write_bytes(b"\x28\xb5\x2f\xfd\xa0" + content_size.to_bytes(4, "little") + b"\x00" * 16)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_archived_sessions_report_exactly_what_is_missing(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        full = self.write(Rollout(SID, t + timedelta(seconds=1)).meta().turn("t1", "complete", "ok"))
        part = self.write(Rollout(SID2, t + timedelta(seconds=1)).meta().turn("t1", "partial", "ok"), SID2)
        self.collect()
        size = full.stat().st_size
        full.unlink()
        self.zst(full.name, size)                       # archived after we read all of it
        part_size = part.stat().st_size
        part.unlink()
        self.zst(part.name, part_size + 5000)           # Codex appended 5000 more bytes before archiving
        self.zst(name_for("01a20000-0000-7000-8000-00000000000f"), 1234)  # archived before we ever saw it
        self.zst(name_for("01a20000-0000-7000-8000-00000000000e"), 999, mtime=(t - timedelta(days=3)).timestamp())
        self.collect()
        states = dict(self.sql("SELECT name, state FROM files"))
        self.assertEqual(states[full.name], "archived_complete")
        self.assertEqual(states[part.name], "archived_partial")
        missing = {(i[1], i[4]) for i in self.issues() if i[0] == "missing"}
        self.assertIn(("archived_compressed", 5000), missing)
        self.assertEqual(sum(1 for code, _ in missing if code == "archived_unread"), 1)  # the old archive is ignored
        status = self.run_exe("status")["collector"]
        self.assertEqual(status["missing"]["archived_compressed"], 5000)

    def test_old_or_unknown_formats_are_reported_not_guessed(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        r = Rollout(SID, t + timedelta(seconds=1)).meta().old_format_turn("old prompt", "old reply")
        r.add("brand_new_type", {"x": 1})
        r.ordinal += 3  # three records lost
        r.turn("t1", "new prompt", "new reply")
        path = self.write(r)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{not json}\n")
        self.collect()
        self.assertNotIn("old prompt", self.blob())  # unmapped: never guessed into a prompt
        unrec = dict(self.sql("SELECT category, count FROM skipped WHERE category LIKE 'unrecognized:%'"))
        self.assertEqual(unrec, {"unrecognized:event_msg/user_message": 1, "unrecognized:event_msg/agent_message": 1,
                                 "unrecognized:brand_new_type": 1})
        codes = {i[1]: i[4] for i in self.issues()}
        self.assertEqual(codes.get("ordinal_gap"), 3)
        self.assertIn("bad_json", codes)
        p = subprocess.Popen([str(EXE), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env)
        try:
            p.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"recording_status","arguments":{}}}\n')
            p.stdin.flush()
            text = json.loads(p.stdout.readline())["result"]["content"][0]["text"]
        finally:
            p.stdin.close()
            p.wait(10)
            p.stdout.close()
        self.assertIn("格式未识别", text)
        self.assertIn("缺失或无法解析", text)

    def test_concurrent_collectors_never_duplicate(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        for i in range(5):
            sid = f"01a20000-0000-7000-8000-0000000001{i:02d}"
            r = Rollout(sid, t + timedelta(seconds=1)).meta()
            for k in range(60):
                r.turn(f"t{k}", f"prompt {i}-{k}", "reply", command="echo hi")
            self.write(r, sid)
        env = {**self.env, "ASSIST_ALL_COLLECT": "1"}  # bypass the leader mutex: every process writes
        procs = [subprocess.Popen([str(EXE), "collect-once"], stdout=subprocess.PIPE, env=env) for _ in range(6)]
        outs = [json.loads(p.communicate(timeout=180)[0].decode().strip().splitlines()[-1]) for p in procs]
        self.collect()
        (n,), = self.sql("SELECT COUNT(*) FROM events")
        (distinct,), = self.sql("SELECT COUNT(DISTINCT event_key) FROM events")
        self.assertEqual(n, distinct)
        self.assertEqual(n, 5 * (1 + 60 * (sum(TURN_KINDS.values()) + sum(TOOL_KINDS.values()))))
        self.assertEqual(sum(o.get("inserted", 0) for o in outs), n)  # each event inserted by exactly one process
        self.assertTrue(all(o.get("state") in ("on", None) or "skipped" in o for o in outs), outs)
        self.assertEqual(self.sql("SELECT last_error FROM collector")[0][0], None)

    def test_revocation_mid_pass_stops_all_writes(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        for i in range(4):
            sid = f"01a20000-0000-7000-8000-0000000002{i:02d}"
            r = Rollout(sid, t + timedelta(seconds=1)).meta()
            for k in range(400):
                r.turn(f"t{k}", f"p {k}", "reply", command="echo")
            self.write(r, sid)
        p = subprocess.Popen([str(EXE), "collect-once"], stdout=subprocess.PIPE, env=self.env)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and self.sql("SELECT COUNT(*) FROM events")[0][0] == 0:
            time.sleep(0.02)
        paused = self.run_exe("pause")
        out = json.loads(p.communicate(timeout=120)[0].decode().strip().splitlines()[-1])
        paused_at = paused["periods"][-1]["to"]
        (last,), = self.sql("SELECT MAX(collected_at) FROM events")
        (total,), = self.sql("SELECT COUNT(*) FROM events")
        self.assertEqual(out["state"], "consent_changed", out)
        self.assertLessEqual(last, paused_at)
        self.assertLess(total, 4 * (1 + 400 * 10))  # it really was interrupted
        before = total
        self.collect()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM events")[0][0], before)

    def test_disabling_the_plugin_excludes_the_disabled_time(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        config = self.home / "config.toml"
        r = Rollout(SID, t + timedelta(seconds=1), step_ms=1).meta().turn("t1", "before disable", "ok")
        self.write(r)
        time.sleep(0.2)
        config.write_text('[plugins."codex-native-assist@ctr-local"]\nenabled = false\n', encoding="utf-8")
        time.sleep(0.2)
        self.write(r.at(utc_now()).turn("t2", "while disabled", "ok"))
        self.assertEqual(self.collect()["state"], "plugin_disabled")
        self.assertEqual(self.events(), [])  # nothing at all while disabled
        self.assertTrue(self.run_exe("status")["recording"]["suspended"])
        time.sleep(0.2)
        config.write_text('[plugins."codex-native-assist@ctr-local"]\nenabled = true\n', encoding="utf-8")
        time.sleep(0.2)
        self.write(r.at(utc_now()).turn("t3", "after re-enable", "ok"))
        self.collect()
        prompts = [e["data"]["text"] for e in self.events() if e["kind"] == "user_prompt"]
        self.assertEqual(prompts, ["before disable", "after re-enable"])  # the disabled time is never collected
        self.assertFalse(self.run_exe("status")["recording"]["suspended"])
        (n,), = self.sql("SELECT COUNT(*) FROM consent_periods")
        self.assertEqual(n, 2)

    def _gap(self, config_changed: bool, last_state: str = "on"):
        t = self.now - timedelta(minutes=10)
        self.consent([(t, None)])
        config = self.home / "config.toml"
        config.write_text('[plugins."codex-native-assist@ctr-local"]\nenabled = true\n', encoding="utf-8")
        os.utime(config, ((t + timedelta(seconds=30)).timestamp(),) * 2)
        r = Rollout(SID, t + timedelta(seconds=40), step_ms=1).meta().turn("t1", "watched", "ok")
        self.write(r)
        self.collect()
        last = self.now - timedelta(minutes=5)  # the last collector was killed here (e.g. by the App's switch)
        self.sql("UPDATE collector SET state = ?, last_pass = ? WHERE id = 1", (last_state, iso(last)))
        self.write(r.at(self.now - timedelta(minutes=3)).turn("t2", "unwatched", "ok"))
        if config_changed:  # the plugin was switched off and on meanwhile
            config.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
            os.utime(config, ((self.now - timedelta(minutes=1)).timestamp(),) * 2)
        self.write(r.at(utc_now()).turn("t3", "after", "ok"))
        self.collect()
        return [e["data"]["text"] for e in self.events() if e["kind"] == "user_prompt"]

    def test_unwatched_gap_with_config_change_is_excluded(self):
        self.assertEqual(self._gap(config_changed=True), ["watched", "after"])
        (n,), = self.sql("SELECT COUNT(*) FROM consent_periods")
        self.assertEqual(n, 2)
        self.assertIn("consent_gap_excluded", [e["event"] for e in self.lifecycle()])

    def test_orderly_shutdown_gap_is_caught_up_despite_config_change(self):
        # The App rewrites config.toml at startup; after a clean quit ("stopped") that must not cost catch-up.
        self.assertEqual(self._gap(config_changed=True, last_state="stopped"), ["watched", "unwatched", "after"])

    def test_leader_marks_stopped_on_stdin_eof(self):
        self.consent([(self.now - timedelta(minutes=1), None)])
        p = subprocess.Popen([str(EXE), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.sql("SELECT state FROM collector")[0][0] != "on":
            time.sleep(0.2)
        p.stdin.close()
        p.wait(10)
        p.stdout.close()
        self.assertEqual(self.sql("SELECT state, last_error FROM collector")[0], ("stopped", "stdin_eof"))

    def test_unwatched_gap_without_config_change_is_caught_up(self):
        self.assertEqual(self._gap(config_changed=False), ["watched", "unwatched", "after"])

    def test_delete_data_keeps_offsets(self):
        t = self.now - timedelta(minutes=5)
        self.consent([(t, None)])
        self.write(Rollout(SID, t + timedelta(seconds=1)).meta().turn("t1", "secret work", "ok"))
        self.collect()
        self.assertGreater(len(self.events()), 0)
        subprocess.run([str(EXE), "delete-data"], env=self.env, capture_output=True, timeout=60)
        self.collect()
        self.assertEqual(self.events(), [])  # not re-read: offsets are kept, periods restart now


class MockSync:
    """The sync backend of docs/sync-api.md, in-process (scripts/mock_sync_server.py)."""

    def __init__(self):
        self.server = mock_sync_server.make_server("127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method, headers={"Content-Type": "application/json"},
                                     data=json.dumps(body).encode("utf-8") if body is not None else None)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as resp:
            return json.loads(resp.read() or b"{}")

    def events(self) -> list[dict]:
        return self.call("GET", "/_mock/events")["events"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class SyncTest(Base):
    """Upload after each pass; an event leaves the local store only once the backend has it."""

    def setUp(self):
        super().setUp()
        self.backend = MockSync()
        self.addCleanup(self.backend.stop)
        self.synced = {**self.env, "ASSIST_SYNC_URL": self.backend.url, "NO_PROXY": "127.0.0.1,localhost"}
        t = utc_now() - timedelta(minutes=10)
        self.consent([(t, None)])
        (self.day / name_for(SID)).write_text(
            Rollout(SID, t + timedelta(seconds=1)).meta().turn("t1", "list files", "done", command="dir")
            .turn("t2", "again", "done again").text(), encoding="utf-8")

    def collect(self, env: dict | None = None) -> dict:
        return self.run_exe("collect-once", env=env or self.synced)

    def local(self) -> int:
        return self.sql("SELECT COUNT(*) FROM events")[0][0]

    def test_uploads_then_deletes_locally(self):
        kept = self.collect(self.env)  # no address configured: everything stays local
        self.assertEqual(kept["sync"]["state"], "not_configured")
        n = self.local()
        self.assertGreater(n, 5)
        keys = {k for (k,) in self.sql("SELECT event_key FROM events")}

        res = self.collect()
        got = self.backend.events()
        self.assertEqual({e["event_id"] for e in got}, keys)
        self.assertTrue(all(e["schema"] == "csr.event/v1" and isinstance(e["seq"], int) for e in got))
        self.assertEqual(self.local(), 0)
        self.assertEqual((res["sync"]["uploaded"], res["sync"]["state"]), (n, "idle"))
        # Upload order within a session follows the session (ordinal, idx).
        seqs = [e["seq"] for e in sorted(got, key=lambda e: e["_arrival"])]
        self.assertEqual(seqs, sorted(seqs))
        # The device token is DPAPI-protected on disk, never in clear text.
        vault = (self.data / "sync.bin").read_bytes()
        self.assertNotIn(b"ctrd_", vault)
        self.assertNotIn("ctrd_", (self.data / "lifecycle.jsonl").read_text(encoding="utf-8"))
        # Nothing is sent twice.
        self.collect()
        self.assertEqual(len(self.backend.events()), n)
        status = self.run_exe("status", env=self.synced)
        self.assertEqual(status["collector"]["sync"]["uploaded"], n)

    def test_recording_off_uploads_nothing(self):
        self.collect(self.env)
        n = self.local()
        subprocess.run([str(EXE), "pause"], env=self.env, capture_output=True, timeout=60)
        self.collect()
        self.assertEqual(self.backend.events(), [])
        self.assertEqual(self.local(), n)

    def test_failure_keeps_events_for_next_time(self):
        self.backend.call("POST", "/_mock/fail-next", {"status": 503, "count": 1, "path": "/v1/events/batch"})
        res = self.collect()
        n = self.local()
        self.assertGreater(n, 0)
        self.assertEqual(res["sync"]["state"], "retrying")
        self.assertTrue(res["sync"]["last_error"])  # the error code only, never a body
        self.assertEqual(self.backend.events(), [])
        res = self.collect()
        self.assertEqual(len(self.backend.events()), n)
        self.assertEqual(self.local(), 0)

    def test_lost_answer_resent_and_deduplicated(self):
        # The backend stores the batch but the answer never arrives: the events stay local, are sent again,
        # come back as duplicates, and only then are deleted.
        self.backend.call("POST", "/_mock/fail-next", {"mode": "drop_after_store"})
        self.collect()
        n = self.local()
        self.assertGreater(n, 0)
        self.assertEqual(len(self.backend.events()), n)
        res = self.collect()
        self.assertEqual(self.local(), 0)
        self.assertEqual(len(self.backend.events()), n)
        self.assertEqual(res["sync"]["duplicates"], n)

    def test_revoked_device_stops_uploading(self):
        self.collect()
        self.backend.call("POST", "/_mock/revoke-all")
        Rollout_text = (self.day / name_for(SID)).read_text(encoding="utf-8")
        (self.day / name_for(SID2)).write_text(Rollout_text.replace(SID, SID2), encoding="utf-8")
        res = self.collect()
        self.assertEqual(res["sync"]["state"], "revoked")
        self.assertGreater(self.local(), 0)  # kept locally, not lost


class FlowTest(Base):
    def setUp(self):
        super().setUp()
        self.backend = MockBackend()
        self.backend.state.slow_seconds = 2.5
        self.env.update(ASSIST_TEST_MODE="1", ASSIST_RECHARGE_URL=self.backend.url, NO_PROXY="127.0.0.1,localhost")

    def tearDown(self):
        self.backend.stop()
        super().tearDown()

    def flow(self, cdks, account=ACCOUNT, **opts) -> dict:
        spec = {"account": account, "cdks": cdks, "retry_ms": [50, 50, 50], "poll_ms": 100, "poll_timeout_ms": 3000,
                "timeout_ms": 1500, **opts}
        return self.run_exe("flow-test", stdin=json.dumps(spec))

    def kinds(self, out) -> list[str]:
        return [o["kind"] for o in out["outcomes"]]

    def test_bind_then_outcomes(self):
        out = self.flow(["TEST-OK01", "TEST-OK01", "bad cdk 中文", "TEST-NOPE", "TEST-EXPD", "TEST-FAIL", "TEST-PROC", "TEST-PFAL"])
        self.assertEqual([v["state"] for v in out["views"]], ["need_bind", "ready"])
        self.assertEqual(self.kinds(out), ["succeeded", "replayed", "invalid_format", "cdk_invalid", "cdk_expired",
                                           "fulfillment_failed", "succeeded", "fulfillment_failed"])
        self.assertTrue(out["bound"])
        self.assertIn("ChatGPT Plus", out["outcomes"][0]["product"])
        self.assertIn("TE…01", out["outcomes"][0]["message"])  # only a mask is ever shown
        self.assertNotIn("TEST-OK01", json.dumps(out["outcomes"], ensure_ascii=False))
        log = (self.data / "recharge.log").read_text(encoding="utf-8")
        self.assertNotIn("TEST-", log)
        self.assertNotIn("user@example.com", log)

    def test_existing_binding_and_account_change(self):
        self.flow(["TEST-OK02"])
        self.assertEqual(self.flow([])["views"][0]["state"], "ready")
        other = dict(ACCOUNT, id="acct-0002", email="other@example.com")
        self.assertEqual(self.flow([], account=other, bind=False)["views"][0]["state"], "account_changed")
        self.assertEqual(self.flow([], account=None)["views"][0]["state"], "no_account")

    def test_timeout_then_idempotent_retry_counts_as_success(self):
        out = self.flow(["TEST-SLOW"])
        self.assertEqual(self.kinds(out), ["succeeded"])
        self.assertTrue(any("重试" in p for p in out["progress"]))
        self.assertGreaterEqual(sum(1 for p in self.backend.state.paths if p == "/v1/redemptions"), 2)

    def test_binding_expired_drops_binding(self):
        self.flow([])
        self.backend.post("/_mock/expire-bindings")
        out = self.flow(["TEST-OK03"], bind=False)
        self.assertEqual(self.kinds(out), ["binding_invalid"])
        self.assertFalse(out["bound"])

    def test_server_errors_rate_limit_and_redirect(self):
        self.flow([])
        self.backend.post("/_mock/fail-next", {"status": 503})
        self.assertEqual(self.kinds(self.flow(["TEST-OK04"], bind=False)), ["server_error"])
        self.backend.post("/_mock/fail-next", {"status": 429, "retry_after": 7})
        out = self.flow(["TEST-OK04"], bind=False)
        self.assertEqual(self.kinds(out), ["rate_limited"])
        self.assertIn("7 秒", out["outcomes"][0]["message"])
        self.backend.post("/_mock/fail-next", {"status": 303, "location": "http://127.0.0.1:9/elsewhere"})
        out = self.flow(["TEST-OK05"], bind=False, retry_ms=[50])
        # A redirected POST may have been processed: never "not used"; the retry settles it idempotently.
        self.assertIn(self.kinds(out)[0], ("unknown", "succeeded"))
        self.assertNotIn("Authorization", json.dumps(self.backend.state.paths))

    def test_not_sent_when_backend_unreachable(self):
        self.flow([])  # bound
        self.backend.stop()  # nothing listens on that port any more: the request cannot have been sent
        out = self.flow(["TEST-OK01"], bind=False)
        self.assertEqual(self.kinds(out), ["not_sent"])
        self.assertIn("CDK 未使用", out["outcomes"][0]["message"])

    def test_insecure_remote_url_refused(self):
        self.env["ASSIST_RECHARGE_URL"] = "http://recharge.example.com"
        out = self.flow(["TEST-OK01"])
        self.assertEqual(out["views"][0]["state"], "not_configured")
        self.assertIn("https", out["views"][0]["message"])
        self.assertEqual(self.backend.state.paths, [])

    def test_test_entry_is_disabled_without_test_mode(self):
        env = dict(self.env)
        env.pop("ASSIST_TEST_MODE")
        proc = subprocess.run([str(EXE), "flow-test"], input=b"{}", capture_output=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")


if __name__ == "__main__":
    unittest.main()
