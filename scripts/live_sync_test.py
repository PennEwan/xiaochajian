"""Live acceptance for data sync (P3) against the Codex App kernel. Consumes model quota.

Prerequisites: the recorder's hooks are trusted (ctr status shows "trusted 12") and recording
is on for the test bed (ctr enable --i-consent --cwd D:\\ctr-testbed).

Starts scripts/mock_sync_server.py on 127.0.0.1:18788 (state in .testdata/live-sync/mock.json),
points `ctr sync` at it and runs, through our own `codex app-server` (same kernel and protocol
as the desktop App):
  A. a turn with a shell command, then a turn on another model     (acceptance 1, 3, 6)
  B. the recharge window is not running while A uploads            (acceptance 2)
  C. backend down -> a turn -> backend back + a lost response      (acceptance 5)
  D. sync paused -> a turn -> nothing new reaches the backend      (acceptance 4)
  E. revoke with delete -> backend data and device gone
Results: .testdata/live-sync/results.json

    python scripts/live_sync_test.py --alt-model gpt-5.6-luna
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "codex-task-recorder" / "lib"))

from ctr import kernel, store  # noqa: E402
from ctr.appserver import AppServer  # noqa: E402
from ctr.paths import PLUGIN_SELECTOR, Paths  # noqa: E402
from ctr.sync import launcher  # noqa: E402
from ctr.sync import state as sstate  # noqa: E402
from ctr.sync.config import SyncPaths  # noqa: E402
from ctr.sync.outbox import Outbox, skip_reason  # noqa: E402
from ctr.sync.worker import read_status  # noqa: E402

TESTBED = r"D:\ctr-testbed"
OUT = REPO / ".testdata" / "live-sync"
PORT = 18788
URL = f"http://127.0.0.1:{PORT}"
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PATHS = Paths.default()
SP = SyncPaths(PATHS)


def tail(text: str, n: int = 3) -> str:
    return "\n".join(text.splitlines()[-n:])


def ctr(*args: str) -> str:
    proc = subprocess.run([sys.executable, "-m", "ctr", *args], cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8",
                               "PYTHONPATH": str(REPO / "plugins/codex-task-recorder/lib")})
    return (proc.stdout + proc.stderr).strip()


class Mock:
    def __init__(self):
        self.proc = None

    def start(self) -> None:
        log = open(OUT / "mock.log", "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(REPO / "scripts" / "mock_sync_server.py"),
                                      "--port", str(PORT), "--store", str(OUT / "mock.json")],
                                     stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.get("/_mock/stats")
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("mock backend did not start")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None

    def get(self, path: str) -> dict:
        with DIRECT.open(URL + path, timeout=5) as resp:
            return json.loads(resp.read())

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with DIRECT.open(req, timeout=5) as resp:
            return json.loads(resp.read())

    def events(self, **match) -> list[dict]:
        return [e for e in self.get("/_mock/events")["events"] if all(e.get(k) == v for k, v in match.items())]


def text_input(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "text_elements": []}]


def run_turn(s: AppServer, thread_id: str, text: str, **overrides) -> dict:
    mark = s.mark()
    res = s.request("turn/start", {"threadId": thread_id, "input": text_input(text), **overrides})
    turn_id = res["turn"]["id"]
    done = s.wait_for(lambda m: m.get("method") == "turn/completed"
                      and m["params"]["turn"]["id"] == turn_id, timeout=600, after=mark)
    return done["params"]["turn"]


def wait(pred, timeout: float, what: str, step: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(step)
    raise TimeoutError(what)


def local_uploadable(session_id: str) -> set[str]:
    st = sstate.load(SP)
    return {e["event_id"] for e in store.iter_events(PATHS)
            if e.get("session_id") == session_id and st and skip_reason(st, e) is None}


def backlog_empty() -> bool:
    st = sstate.load(SP)
    return Outbox(PATHS).backlog(st) == (0, 0)


def recharge_window_running() -> bool:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenMutexW.restype = ctypes.c_void_p
    handle = k.OpenMutexW(0x00100000, False, "Local\\CodexCdkRecharge.Window")  # SYNCHRONIZE
    if handle:
        k.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def turn_summary(mock: Mock, turn_id: str) -> dict:
    remote = mock.events(turn_id=turn_id)
    coverage = next((e for e in remote if e["kind"] == "turn_coverage"), None)
    hook_calls = [e["tool_use_id"] for e in remote if e["kind"] == "tool_call" and e["source"]["type"] == "hook"]
    ui_items = [e["item_id"] for e in remote if e["kind"] == "tool_result" and e["granularity"] == "ui_item"]
    return {"turn_id": turn_id, "remote_kinds": dict(Counter(e["kind"] for e in remote)),
            "models": sorted({e["model"] for e in remote if e.get("model")}),
            "hook_tool_use_ids": hook_calls, "transcript_ui_item_ids": ui_items,
            "hook_calls_matched": sorted(set(hook_calls) & set(ui_items)),
            "coverage": coverage["data"] if coverage else None}


def hook_durations(s: AppServer, event: str) -> list[int]:
    """End-to-end ms of the recorder's synchronous hooks (app-server names them camelCase;
    the recharge plugin's hooks show up in the same stream and are left out)."""
    return [m["params"]["run"].get("durationMs") for m in s.notifications
            if m.get("method") == "hook/completed" and m["params"]["run"].get("eventName") == event
            and "codex-task-recorder" in (m["params"]["run"].get("sourcePath") or "")]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--alt-model", default="gpt-5.6-luna")
    ap.add_argument("--effort", default="low")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("mock.json", "mock.log"):
        (OUT / name).unlink(missing_ok=True)
    results: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    mock = Mock()
    mock.start()
    exe, version = kernel.find_app_kernel()
    results["kernel_version"] = version
    print("kernel:", exe, version)

    print(tail(ctr("install"), 1))
    print(tail(ctr("sync", "config", "--base-url", URL), 1))
    print(tail(ctr("sync", "enable", "--i-consent", "--skip-install")))

    s = AppServer(exe, TESTBED, OUT / "appserver.jsonl")
    try:
        s.initialize()
        trust = Counter(h["trustStatus"] for h in s.plugin_hooks(TESTBED, PLUGIN_SELECTOR))
        results["hook_trust_after_install"] = dict(trust)
        print("hook trust:", dict(trust))
        if trust.get("trusted", 0) != 12:
            print("Hooks are not all trusted; trust them in the Codex App first.")
            return 2
        tid = s.request("thread/start", {"cwd": TESTBED, "approvalPolicy": "never",
                                         "sandbox": "workspace-write"})["thread"]["id"]
        results["thread_id"] = tid
        common = {"effort": args.effort, "summary": "detailed"}

        # A + B
        t1 = run_turn(s, tid, "请在当前目录运行命令 `echo sync-ok`，然后用一句话报告输出。", **common)
        results["B_recharge_window_running_during_A"] = recharge_window_running()
        results["B_uploader_running_during_A"] = launcher.worker_running(SP)
        t2 = run_turn(s, tid, "用一句话复述上一步命令的输出。", model=args.alt_model, **common)
        wait(lambda: mock.events(turn_id=t2["id"], kind="turn_coverage"), 180, "turn 2 uploaded")
        wait(backlog_empty, 120, "backlog drained")
        results["A_turns"] = [turn_summary(mock, t1["id"]), turn_summary(mock, t2["id"])]
        changed = [(e["data"]["from"], e["data"]["to"]) for e in mock.events(session_id=tid, kind="model_changed")]
        results["A_model_changed"] = changed
        remote_ids = {e["event_id"] for e in mock.events(session_id=tid)}
        local_ids = local_uploadable(tid)
        results["A_local_vs_remote"] = {"local": len(local_ids), "remote": len(remote_ids),
                                        "missing_remote": len(local_ids - remote_ids),
                                        "unexpected_remote": len(remote_ids - local_ids)}
        results["A_capabilities"] = mock.get("/_mock/envelopes")["envelopes"][0].get("capabilities")
        results["A_sync_status"] = ctr("sync", "status")
        print("[A]", results["A_turns"], results["A_local_vs_remote"])

        # C: backend down, one turn, backend back with a lost response
        mock.stop()
        t3 = run_turn(s, tid, "回复：offline-ok", effort=args.effort)
        time.sleep(8)
        results["C_while_down"] = {"remote_reachable": False, "status_last_error": read_status(SP).get("last_error"),
                                   "backlog": Outbox(PATHS).backlog(sstate.load(SP))}
        mock.start()
        mock.post("/_mock/fail-next", {"mode": "drop_after_store"})
        wait(lambda: mock.events(turn_id=t3["id"], kind="turn_coverage"), 300, "turn 3 uploaded after restart")
        wait(backlog_empty, 120, "backlog drained after restart")
        remote = mock.events(session_id=tid)
        ids = [e["event_id"] for e in remote]
        stats = mock.get("/_mock/stats")["stats"]
        results["C_after_recovery"] = {
            "remote_events": len(ids), "unique": len(set(ids)),
            "duplicates_seen_by_backend": sum(v["duplicates"] for v in stats.values()),
            "missing_remote": len(local_uploadable(tid) - set(ids)),
            "turn3": turn_summary(mock, t3["id"])}
        by_arrival = sorted(remote, key=lambda e: e["_arrival"])
        results["C_seq_in_arrival_order"] = [e["seq"] for e in by_arrival] == sorted(e["seq"] for e in by_arrival)
        print("[C]", results["C_while_down"], results["C_after_recovery"])

        # D: paused
        print(tail(ctr("sync", "pause")))
        t4 = run_turn(s, tid, "回复：paused-ok", effort=args.effort)
        time.sleep(15)
        local_t4 = [e for e in store.iter_events(PATHS) if e.get("turn_id") == t4["id"]]
        results["D_paused"] = {"remote_events_for_turn": len(mock.events(turn_id=t4["id"])),
                               "local_events_for_turn": len(local_t4)}
        print(tail(ctr("sync", "enable", "--i-consent", "--skip-install")))
        launcher.kick(PATHS, force=True)
        time.sleep(15)
        results["D_after_resume_remote_events_for_paused_turn"] = len(mock.events(turn_id=t4["id"]))
        print("[D]", results["D_paused"], results["D_after_resume_remote_events_for_paused_turn"])

        results["hook_ms"] = {name: hook_durations(s, name) for name in ("userPromptSubmit", "interrupt")}
        recorder_ms = [e["data"]["duration_ms"] for e in store.iter_events(PATHS)
                       if e.get("session_id") == tid and e["kind"] == "hook_invocation"]
        results["recorder_ms"] = {"n": len(recorder_ms), "max": max(recorder_ms),
                                  "median": sorted(recorder_ms)[len(recorder_ms) // 2]}
    finally:
        s.close()

    # E: revoke and delete
    time.sleep(3)
    results["E_revoke_output"] = ctr("sync", "revoke", "--yes", "--delete-remote")
    stats = mock.get("/_mock/stats")
    results["E_after_revoke"] = {"backend_devices": stats["devices"], "backend_events": stats["events"],
                                 "pending_ops": sstate.load(SP).pending_ops}
    print("[E]", results["E_after_revoke"])
    mock.stop()
    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("results:", OUT / "results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
