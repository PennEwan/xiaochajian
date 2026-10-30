"""Task 1 lifecycle probe: how does the Codex App kernel run a plugin-bundled MCP server?

Uses the App's own kernel (`codex app-server`, same protocol and process model as the desktop App)
in a throwaway CODEX_HOME with a mock model, so it needs no login or quota and never touches
~/.codex. The plugin under test is a copy of plugins/codex-native-assist whose .mcp.json also
forwards the ASSIST_* test variables.

    python scripts\\native_lifecycle_probe.py [--idle 120]

Evidence comes from two independent sources: the server's own lifecycle log (start / heartbeat /
exit reason) and the OS process table (assist.exe processes under the throwaway directory).
Results: .testdata/native-probe/results.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "codex-task-recorder" / "lib"))
from ctr import kernel  # noqa: E402
from ctr.appserver import AppServer  # noqa: E402

WORK = Path(os.environ.get("NAT_PROBE_DIR", r"D:\tmp\nat-probe"))
HOME, DATA, MARKET, BED = WORK / "codex-home", WORK / "assist-data", WORK / "market", WORK / "bed"
OUT = REPO / ".testdata" / "native-probe"
MARKET_NAME, PLUGIN = "nat-probe", "codex-native-assist"
PID = f"{PLUGIN}@{MARKET_NAME}"
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CONFIG = '''model = "mock-model"
model_provider = "mock"
model_reasoning_effort = "low"
approval_policy = "never"

[model_providers.mock]
name = "Mock"
base_url = "http://127.0.0.1:18790/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
'''
RESULTS: dict = {"steps": []}


def step(name: str, **evidence) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {name}: {json.dumps(evidence, ensure_ascii=False, default=str)[:600]}", flush=True)
    RESULTS["steps"].append({"step": name, "at": time.strftime("%H:%M:%S"), **evidence})


def assist_procs() -> list[dict]:
    """Ground truth from the OS: assist.exe processes started from the throwaway plugin cache."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'assist%.exe'\" | "
          "Select-Object ProcessId,ParentProcessId,ExecutablePath,CreationDate | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout.strip()
    rows = json.loads(out) if out else []
    rows = rows if isinstance(rows, list) else [rows]
    return [{"pid": r["ProcessId"], "ppid": r["ParentProcessId"]} for r in rows
            if str(r.get("ExecutablePath") or "").lower().startswith(str(WORK).lower())]


def lifecycle(since: int = 0) -> list[dict]:
    path = DATA / "lifecycle.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[since:]


def events(rows: list[dict], *names: str) -> list[dict]:
    keep = ("pid", "event", "reason", "ppid", "parent", "grandparent", "in_job", "tool", "client", "staying",
            "uptime_sec", "cwd", "PLUGIN_ROOT", "PLUGIN_DATA", "CODEX_HOME", "method")
    return [{k: r[k] for k in keep if k in r} for r in rows if not names or r["event"] in names]


def wait_until(pred, timeout: float, step_s: float = 0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(step_s)
    return pred()


def prepare(exe: Path) -> None:
    for p in assist_procs():
        subprocess.run(["taskkill", "/F", "/PID", str(p["pid"])], capture_output=True)
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    for d in (HOME, DATA, BED):
        d.mkdir(parents=True, exist_ok=True)
    (HOME / "config.toml").write_text(CONFIG, encoding="utf-8")
    plugin = MARKET / "plugins" / PLUGIN
    shutil.copytree(REPO / "plugins" / PLUGIN, plugin)
    mcp = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp["mcpServers"]["native_assist"]
    server["env_vars"] += ["ASSIST_DATA_DIR", "ASSIST_HEARTBEAT_SEC", "ASSIST_NO_ORPHAN_EXIT", "ASSIST_COLLECT_MS",
                           "ASSIST_RECORDING_DEFAULT"]
    (plugin / ".mcp.json").write_text(json.dumps(mcp, indent=2), encoding="utf-8")
    (MARKET / ".agents" / "plugins").mkdir(parents=True)
    (MARKET / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps({
        "name": MARKET_NAME, "interface": {"displayName": "native probe"},
        "plugins": [{"name": PLUGIN, "source": {"source": "local", "path": f"./plugins/{PLUGIN}"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Productivity"}]}), encoding="utf-8")
    os.environ.update({"NO_PROXY": "127.0.0.1,localhost", "CODEX_HOME": str(HOME), "ASSIST_DATA_DIR": str(DATA), "ASSIST_HEARTBEAT_SEC": "5",
                       "ASSIST_RECORDING_DEFAULT": "off",  # release behaviour: off until consent
                       "ASSIST_NO_ORPHAN_EXIT": "1", "ASSIST_COLLECT_MS": "1000"})
    before = (HOME / "config.toml").read_text(encoding="utf-8")
    r1 = kernel.run(exe, ["plugin", "marketplace", "add", str(MARKET)])
    r2 = kernel.run(exe, ["plugin", "add", PID])
    after = (HOME / "config.toml").read_text(encoding="utf-8")
    cache = sorted(str(p.relative_to(HOME)) for p in (HOME / "plugins" / "cache").rglob("*") if p.is_file())
    step("install", marketplace_rc=r1.returncode, add_rc=r2.returncode, add_out=(r2.stdout + r2.stderr).strip()[-400:],
         config_added=after[len(before):].strip(), cache_files=cache,
         procs_after_install=assist_procs())


class Kernel:
    def __init__(self, exe: Path):
        self.exe = exe
        self.s: AppServer | None = None
        self.starts = 0

    def start(self) -> AppServer:
        self.starts += 1
        self.s = AppServer(self.exe, str(BED), OUT / f"appserver-{self.starts}.jsonl")
        self.s.initialize()
        return self.s

    def thread(self, approval: str = "never") -> str:
        return self.s.request("thread/start", {"cwd": str(BED), "approvalPolicy": approval,
                                               "sandbox": "workspace-write"})["thread"]["id"]

    def turn(self, thread: str, text: str, timeout: float = 120) -> dict:
        mark = self.s.mark()
        res = self.s.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": text,
                                                                          "text_elements": []}]})
        tid = res["turn"]["id"]
        approvals = []
        end = time.monotonic() + timeout
        done = None
        while time.monotonic() < end and done is None:
            while not self.s.server_requests.empty():
                req = self.s.server_requests.get()
                approvals.append({"method": req.get("method"), "params": req.get("params")})
                self.s.respond(req["id"], {"decision": "accept", "action": "accept", "content": {}})
            for m in self.s.notifications[mark:]:
                if m.get("method") == "turn/completed" and m["params"]["turn"]["id"] == tid:
                    done = m
            time.sleep(0.2)
        items = [m["params"].get("item") for m in self.s.notifications[mark:] if m.get("method") == "item/completed"]
        mcp = [{"type": i.get("type"), "server": i.get("server"), "tool": i.get("tool"), "status": i.get("status"),
                "error": i.get("error")} for i in items if i and "mcp" in str(i.get("type", "")).lower()]
        errors = [m["params"] for m in self.s.notifications[mark:] if m.get("method") == "error"]
        return {"status": done["params"]["turn"].get("status") if done else "timeout",
                "error": (done["params"]["turn"].get("error") if done else None) or (errors[-1] if errors else None),
                "server_requests": approvals, "mcp_items": mcp}

    def mcp_status(self) -> list:
        try:
            res = self.s.request("mcpServerStatus/list", {}, timeout=60)
        except Exception as exc:  # noqa: BLE001 - schema differs across versions; record and move on
            return [f"error: {exc}"]
        data = res.get("data") or res.get("servers") or res
        return [{k: v for k, v in s.items() if k in ("name", "status", "authStatus", "error", "tools")}
                if isinstance(s, dict) else s for s in (data if isinstance(data, list) else [data])]

    def close(self) -> None:
        if self.s:
            self.s.close()
            self.s = None

    def kill(self) -> int:
        pid = self.s.proc.pid
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        self.s = None
        return pid


def store_sql(query: str, args: tuple = ()) -> list:
    """The plugin's shared SQLite store, read with Python's own sqlite3."""
    import sqlite3
    if not (DATA / "assist.sqlite3").exists():
        status()  # creates the schema
    conn = sqlite3.connect(DATA / "assist.sqlite3", timeout=10)
    try:
        rows = conn.execute(query, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def consent_on(scope: str = "all", folders: tuple = ()) -> str:
    """Throwaway data dir only: stands in for the user ticking the box in the consent window."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    store_sql("UPDATE consent_periods SET ended_at = ? WHERE ended_at IS NULL", (now,))
    store_sql("INSERT INTO consent_periods(started_at) VALUES (?)", (now,))
    store_sql("UPDATE consent SET enabled = 1, scope = ?, folders = ?, version = version + 1, changed_by = 'probe' WHERE id = 1",
              (scope, json.dumps(list(folders))))
    return now


def records() -> list[dict]:
    """Stored events, with the prompt / reply text lifted to the top for easy matching."""
    return [json.loads(b) for (b,) in store_sql("SELECT body FROM events ORDER BY collected_at, event_key")]


def status() -> dict:
    exe = next((HOME / "plugins" / "cache" / MARKET_NAME / PLUGIN).glob("*/bin/assist.exe"))
    out = subprocess.run([str(exe), "status"], capture_output=True, encoding="utf-8").stdout
    return json.loads(out)


def mock_set(match: str | None, args: dict | None = None) -> None:
    req = urllib.request.Request("http://127.0.0.1:18790/_mock/call-tool", method="PUT",
                                 data=json.dumps({"match": match, "args": args or {}}).encode())
    DIRECT.open(req, timeout=5).read()


def mock_tools() -> list:
    reqs = json.loads(DIRECT.open("http://127.0.0.1:18790/_mock/requests", timeout=5).read())["requests"]
    return reqs[-1]["tools"] if reqs else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle", type=int, default=120, help="seconds to stay idle with a thread open")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    found = kernel.find_app_kernel()
    if not found:
        print("Codex App kernel not found")
        return 2
    exe, version = found
    RESULTS["kernel"] = {"path": str(exe), "version": version}
    mock = subprocess.Popen([sys.executable, str(REPO / "scripts" / "mock_model_server.py"), "--port", "18790"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        prepare(exe)
        run(exe, args.idle)
    finally:
        mock.kill()
        for p in assist_procs():
            subprocess.run(["taskkill", "/F", "/PID", str(p["pid"])], capture_output=True)
        (OUT / "results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        shutil.copy(DATA / "lifecycle.jsonl", OUT / "lifecycle.jsonl") if (DATA / "lifecycle.jsonl").exists() else None
    print(f"results: {OUT / 'results.json'}")
    return 0


def run(exe: Path, idle: int) -> None:
    k = Kernel(exe)

    # A. app-server up, no thread yet
    n = len(lifecycle())
    s = k.start()
    time.sleep(6)
    step("A app-server initialized, no thread", procs=assist_procs(), lifecycle=events(lifecycle(n)),
         app_server_pid=s.proc.pid)

    # B. first thread
    n = len(lifecycle())
    t1 = k.thread()
    wait_until(lambda: events(lifecycle(n), "initialize"), 30)
    step("B thread/start #1", thread=t1, procs=assist_procs(), lifecycle=events(lifecycle(n), "start", "initialize", "rpc"),
         mcp_status=k.mcp_status())

    # C. a turn where the (mock) model calls our tool
    n = len(lifecycle())
    mock_set("plugin_diagnostics")
    turn = k.turn(t1, "probe: call plugin_diagnostics")
    mock_set(None)
    offered = [t for t in mock_tools() if t and ("native" in t or "assist" in t or "diagnostic" in t)]
    step("C turn calling plugin_diagnostics", turn=turn, offered_tools=offered,
         lifecycle=events(lifecycle(n), "tool_call", "tool_done", "start"), procs=assist_procs())

    # D. idle with the thread open
    n = len(lifecycle())
    pids_before = sorted(p["pid"] for p in assist_procs())
    time.sleep(idle)
    beats = events(lifecycle(n), "heartbeat")
    step(f"D idle {idle}s", procs_before=pids_before, procs_after=sorted(p["pid"] for p in assist_procs()),
         heartbeats=len(beats), heartbeat_pids=sorted({b["pid"] for b in beats}),
         exits=events(lifecycle(n), "exit"))

    # K. recording without hooks: consent on → a turn → records appear while the thread is idle
    consent_on()
    secret = "sk-proj-" + "A1b2C3d4" * 4
    turn = k.turn(t1, f"collector probe: my key is {secret}")
    got = wait_until(lambda: [r for r in records() if "collector probe" in json.dumps(r, ensure_ascii=False)], 15)
    text = json.dumps(records(), ensure_ascii=False)
    step("K consent on (simulated click) → turn → records", turn_status=turn["status"],
         record_kinds=sorted({r["kind"] for r in records()}), prompt_recorded=bool(got),
         secret_in_records=secret in text, redacted_marker="[REDACTED:openai_key]" in text,
         collector=status()["collector"])

    # L. turning it off from Codex: the model calls pause_recording; later turns are not recorded
    mock_set("pause_recording")
    turn = k.turn(t1, "please stop recording")
    mock_set(None)
    after_pause = len(records())
    k.turn(t1, "after pause: this must not be recorded")
    time.sleep(5)
    leaked = [r for r in records() if "must not be recorded" in json.dumps(r, ensure_ascii=False)]
    step("L pause_recording via tool → later turn not recorded", turn=turn, records_at_pause=after_pause,
         records_now=len(records()), leaked_after_pause=len(leaked), recording=status()["recording"]["enabled"])

    # E. second thread
    n = len(lifecycle())
    t2 = k.thread()
    wait_until(lambda: events(lifecycle(n), "initialize"), 30)
    time.sleep(2)
    step("E thread/start #2", thread=t2, procs=assist_procs(), lifecycle=events(lifecycle(n), "start", "initialize"))

    # F. unsubscribe thread #2 (what the App does when a thread is no longer shown)
    n = len(lifecycle())
    try:
        res = s.request("thread/unsubscribe", {"threadId": t2}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        res = f"error: {exc}"
    time.sleep(8)
    step("F thread/unsubscribe #2", result=res, procs=assist_procs(), lifecycle=events(lifecycle(n), "exit", "start"))

    # G. disable the plugin while running (what the plugin page's switch writes)
    n = len(lifecycle())
    consent_on()
    s.request("config/value/write", {"keyPath": f"plugins.{PID}.enabled", "value": False, "mergeStrategy": "replace"})
    time.sleep(8)
    g1 = assist_procs()
    turn = k.turn(t1, "disabled-plugin probe: must not be recorded")
    offered = [t for t in mock_tools() if t and "native_assist" in t]
    time.sleep(4)
    leaked = [r for r in records() if "disabled-plugin probe" in json.dumps(r, ensure_ascii=False)]
    t3 = k.thread()
    time.sleep(8)
    step("G disable plugin (config/value/write) while running", procs_after_disable=g1,
         existing_thread_turn=turn["status"], tools_still_offered_in_existing_thread=offered,
         recorded_after_disable=len(leaked), collector=status()["collector"].get("plugin_disabled"),
         procs_after_new_thread=assist_procs(), lifecycle=events(lifecycle(n), "exit", "start"), new_thread=t3)
    s.request("config/value/write", {"keyPath": f"plugins.{PID}.enabled", "value": True, "mergeStrategy": "replace"})

    # H. graceful shutdown: close app-server stdin
    n = len(lifecycle())
    before = assist_procs()
    k.close()
    gone = wait_until(lambda: not assist_procs(), 20)
    step("H app-server graceful close (stdin EOF)", procs_before=before, all_exited=bool(gone),
         procs_left=assist_procs(), lifecycle=events(lifecycle(n), "exit", "parent_exited"))

    # I. restart: does a fresh app-server bring the server back by itself?
    n = len(lifecycle())
    s = k.start()
    time.sleep(6)
    i0 = assist_procs()
    t4 = k.thread()
    wait_until(lambda: events(lifecycle(n), "initialize"), 30)
    step("I restart app-server, then thread/start", procs_before_thread=i0, procs_after_thread=assist_procs(),
         lifecycle=events(lifecycle(n), "start", "initialize"), thread=t4)

    # J. hard kill of app-server (crash / Task Manager), with a recharge window open
    n = len(lifecycle())
    mock_set("open_recharge_window", {"wait": False})
    t5 = k.thread("on-request")  # the approval the user would click in the App is answered "accept" by turn()
    opened = k.turn(t5, "open the recharge window")
    mock_set(None)
    wait_until(lambda: len(assist_procs()) >= 2, 15)
    before = assist_procs()
    killed = k.kill()
    time.sleep(10)
    left = assist_procs()
    step("J app-server killed (TerminateProcess)", app_server_pid=killed, window_turn=opened["mcp_items"],
         approval_requests=opened["server_requests"],
         procs_before=before, procs_left_after_10s=left,
         lifecycle=events(lifecycle(n), "exit", "parent_exited"),
         note="ASSIST_NO_ORPHAN_EXIT=1: the server logs parent_exited but stays, to see whether Codex cleans it up")


if __name__ == "__main__":
    raise SystemExit(main())
