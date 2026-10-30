"""P4 acceptance on the Codex App kernel: multi-session, stop latency, catch-up, archives, coverage.

Same harness as native_lifecycle_probe.py: the App's own `codex app-server`, a throwaway CODEX_HOME,
the mock model (every first request in a turn runs one shell command, so tool events exist), and a
copy of plugins/codex-native-assist. Nothing touches ~/.codex or the real Codex App.

    python scripts\\native_acceptance.py [--sessions 6] [--latency-runs 5] [--idle 60] [--long-idle 0]

Coverage oracle: the session reader (tools/session-reader, Python) parses the same rollout files
independently; its events inside the consented periods and scope are the expected set.
Results: .testdata/native-acceptance/results.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tools" / "session-reader"))
import native_lifecycle_probe as P  # noqa: E402

OUT = REPO / ".testdata" / "native-acceptance"
RESULTS: dict = {"scenarios": {}}


def record(scenario: str, **evidence) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {scenario}: {json.dumps(evidence, ensure_ascii=False, default=str)[:900]}", flush=True)
    RESULTS["scenarios"][scenario] = {"at": time.strftime("%H:%M:%S"), **evidence}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def memory(pids: list[int]) -> list[dict]:
    if not pids:
        return []
    ps = ("Get-Process -Id " + ",".join(map(str, pids)) + " -ErrorAction SilentlyContinue | "
          "Select-Object Id,WorkingSet64,PrivateMemorySize64,@{n='Cpu';e={$_.TotalProcessorTime.TotalMilliseconds}},"
          "@{n='Threads';e={$_.Threads.Count}},@{n='Handles';e={$_.HandleCount}} | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
    rows = json.loads(out) if out else []
    rows = rows if isinstance(rows, list) else [rows]
    return [{"pid": r["Id"], "working_set_mb": round(r["WorkingSet64"] / 2**20, 1), "private_mb": round(r["PrivateMemorySize64"] / 2**20, 1),
             "cpu_ms": round(r["Cpu"]), "threads": r["Threads"], "handles": r["Handles"]} for r in rows]


def collector() -> dict:
    rows = P.store_sql("SELECT state, state_since, last_pass, last_error, leader_pid FROM collector WHERE id = 1")
    return dict(zip(("state", "state_since", "last_pass", "last_error", "leader_pid"), rows[0]))


def wait_state(state: str, timeout: float = 30) -> dict | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        c = collector()
        if c["state"] == state:
            return c
        time.sleep(0.05)
    return None


def ms_between(a: str, b: str) -> int:
    fa = datetime.strptime(a, "%Y-%m-%dT%H:%M:%S.%fZ")
    fb = datetime.strptime(b, "%Y-%m-%dT%H:%M:%S.%fZ")
    return int((fb - fa).total_seconds() * 1000)


def pause_cli() -> str:
    exe = next((P.HOME / "plugins" / "cache" / P.MARKET_NAME / P.PLUGIN).glob("*/bin/assist.exe"))
    out = subprocess.run([str(exe), "pause"], capture_output=True, encoding="utf-8").stdout
    return json.loads(out.strip().splitlines()[-1])["periods"][-1]["to"]


def events_after(ts: str) -> int:
    return P.store_sql("SELECT COUNT(*) FROM events WHERE collected_at > ?", (ts,))[0][0]


# ---- coverage oracle ---------------------------------------------------------------------------

SCOPED: dict[str, list[str]] = {}  # consent period start -> folders, for periods that were folder-scoped


def oracle() -> dict[str, dict]:
    """Expected events: the session reader's parse of every rollout, limited to consented periods and their scope."""
    from csr.parser import Context, parse_line
    periods = [(a, b) for a, b in P.store_sql("SELECT started_at, ended_at FROM consent_periods")]

    def allowed(ts: str, cwd: str | None) -> bool:
        for a, b in periods:
            if a <= ts and (b is None or ts <= b):
                folders = SCOPED.get(a)
                c = (cwd or "").lower().rstrip("\\") + "\\"
                return folders is None or bool(cwd and any(c.startswith(f.lower().rstrip("\\") + "\\") for f in folders))
        return False
    expected: dict[str, dict] = {}
    for path in sorted(list((P.HOME / "sessions").rglob("rollout-*.jsonl")) + list((P.HOME / "archived_sessions").rglob("rollout-*.jsonl"))):
        sid = path.stem[-36:]
        ctx = Context(session_id=sid)
        cwd = None
        offset = 0
        for raw in path.read_bytes().split(b"\n"):
            if not raw:
                offset += 1
                continue
            parsed = parse_line(raw, offset, ctx, path.name)
            offset += len(raw) + 1
            for e in parsed.events:
                if e["kind"] == "session_meta":
                    cwd = e["data"].get("cwd")
                if e["ts"] and allowed(e["ts"], cwd):
                    expected[e["event_key"]] = e
    return expected


def coverage() -> dict:
    expected = oracle()
    rows = P.store_sql("SELECT event_key, kind, session_id, call_id, body FROM events")
    got = {k: (kind, json.loads(b)) for k, kind, _, _, b in rows}
    matched = [k for k in expected if k in got and got[k][0] == expected[k]["kind"]]
    missing = [k for k in expected if k not in got]
    extra = [k for k in got if k not in expected]
    # semantic duplicates: one model output / tool call stored under two keys
    sem = Counter()
    for k, (kind, body) in got.items():
        if kind in ("user_prompt", "assistant_message", "reasoning_summary", "tool_execution"):
            sem[(body["session_id"], kind, body.get("item_id"))] += 1
        elif kind in ("tool_call", "tool_result"):
            sem[(body["session_id"], kind, body.get("call_id"))] += 1
    dups = sum(n - 1 for n in sem.values() if n > 1)
    by_kind = Counter(e["kind"] for e in expected.values())
    got_kind = Counter(got[k][0] for k in matched)
    return {"expected": len(expected), "stored": len(got), "matched": len(matched), "missing": len(missing),
            "extra": len(extra), "semantic_duplicates": dups,
            "coverage_rate": round(len(matched) / len(expected), 4) if expected else None,
            "missing_rate": round(len(missing) / len(expected), 4) if expected else None,
            "duplicate_rate": round(dups / len(got), 4) if got else 0.0,
            "by_kind": {k: f"{got_kind.get(k, 0)}/{n}" for k, n in sorted(by_kind.items())},
            "missing_keys": missing[:10], "extra_keys": extra[:10]}


# ---- scenarios -----------------------------------------------------------------------------------

def multi_session(k: P.Kernel, n: int) -> list[str]:
    a, b = P.BED / "proj-a", P.BED / "proj-b"
    a.mkdir(exist_ok=True)
    b.mkdir(exist_ok=True)
    consent_from = P.consent_on("folders", (str(a),))
    SCOPED[consent_from] = [str(a)]
    threads = []
    for i in range(n):
        cwd = a if i % 2 == 0 else b
        tid = k.s.request("thread/start", {"cwd": str(cwd), "approvalPolicy": "never", "sandbox": "danger-full-access"})["thread"]["id"]
        threads.append((tid, cwd))
    P.wait_until(lambda: len(P.assist_procs()) >= n, 30)
    for i, (tid, cwd) in enumerate(threads):
        res = k.turn(tid, f"multi-session prompt {i} in {cwd.name}")
        if res["status"] != "completed":
            print("turn failed", res)
    time.sleep(6)
    procs = P.assist_procs()
    mem = memory([p["pid"] for p in procs])
    leaders = sorted({e["pid"] for e in P.lifecycle() if e["event"] == "collector_leader"})
    rows = P.store_sql("SELECT session_id, kind, COUNT(*) FROM events GROUP BY session_id, kind")
    per_session = {}
    for sid, kind, cnt in rows:
        per_session.setdefault(sid, {})[kind] = cnt
    a_threads = {t for t, cwd in threads if cwd == a}
    b_threads = {t for t, cwd in threads if cwd == b}
    keys = P.store_sql("SELECT COUNT(*), COUNT(DISTINCT event_key) FROM events")[0]
    crashes = [e for e in P.lifecycle() if e["event"] in ("crash",)]
    record("multi_session", sessions=n, processes=len(procs), memory=mem,
           memory_total_private_mb=round(sum(m["private_mb"] for m in mem), 1),
           memory_avg_private_mb=round(statistics.mean(m["private_mb"] for m in mem), 1) if mem else None,
           collector_leaders_seen=leaders, collector=collector(),
           recorded_sessions=sorted(per_session), authorized_threads=sorted(a_threads), other_threads=sorted(b_threads),
           scope_isolated=set(per_session) == a_threads and not (set(per_session) & b_threads),
           prompts_per_session={s: c.get("user_prompt") for s, c in per_session.items()},
           tool_calls_per_session={s: (c.get("tool_call"), c.get("tool_execution"), c.get("tool_result")) for s, c in per_session.items()},
           rows=keys[0], distinct_keys=keys[1], sqlite_errors=collector()["last_error"], crashes=crashes,
           coverage=coverage(), consent_from=consent_from)
    return [t for t, _ in threads]


def stop_latency(k: P.Kernel, threads: list[str], runs: int, idle: int) -> None:
    lat_pause, lat_disable, leaked = [], [], []
    P.consent_on()
    time.sleep(idle)
    for r in range(runs):
        P.consent_on()
        assert wait_state("on", 15), "collector never turned on"
        time.sleep(2 + r * 0.37)  # vary the phase against the 2 s poll
        paused_at = pause_cli()
        off = wait_state("off", 15)
        lat_pause.append(ms_between(paused_at, off["state_since"]) if off else None)
        k.turn(threads[0], f"after pause {r}: must not be recorded")
        time.sleep(4)
        leaked.append(events_after(paused_at))
    # disabling the plugin from Codex (what the plugin page's switch writes)
    for r in range(runs):
        P.consent_on()
        assert wait_state("on", 15)
        time.sleep(2 + r * 0.37)
        t0 = now_iso()
        k.s.request("config/value/write", {"keyPath": f"plugins.{P.PID}.enabled", "value": False, "mergeStrategy": "replace"})
        dis = wait_state("plugin_disabled", 15)
        lat_disable.append(ms_between(t0, dis["state_since"]) if dis else None)
        k.turn(threads[0], f"after disable {r}: must not be recorded")
        time.sleep(4)
        leaked.append(events_after(t0))
        k.s.request("config/value/write", {"keyPath": f"plugins.{P.PID}.enabled", "value": True, "mergeStrategy": "replace"})
    texts = json.dumps([json.loads(b) for (b,) in P.store_sql("SELECT body FROM events WHERE kind = 'user_prompt'")], ensure_ascii=False)

    def summary(xs):
        xs = [x for x in xs if x is not None]
        return {"runs": len(xs), "min_ms": min(xs), "avg_ms": round(statistics.mean(xs)), "max_ms": max(xs)} if xs else None
    record("stop_latency", idle_before_s=idle, poll_interval_ms=int(os.environ.get("ASSIST_COLLECT_MS", "2000")),
           pause=summary(lat_pause), pause_samples=lat_pause, disable=summary(lat_disable), disable_samples=lat_disable,
           events_collected_after_stop=leaked, must_not_be_recorded_found="must not be recorded" in texts,
           processes_alive=len(P.assist_procs()))


def long_idle(k: P.Kernel, seconds: int) -> None:
    P.consent_on()
    before = sorted(p["pid"] for p in P.assist_procs())
    mem0 = memory(before)
    time.sleep(seconds)
    after = sorted(p["pid"] for p in P.assist_procs())
    mem1 = memory(after)
    beats = Counter(e["pid"] for e in P.lifecycle() if e["event"] == "heartbeat")
    paused_at = pause_cli()
    off = wait_state("off", 15)
    record("long_idle", idle_s=seconds, processes_before=before, processes_after=after, same_processes=before == after,
           memory_before=mem0, memory_after=mem1, heartbeats=dict(beats),
           pause_latency_ms=ms_between(paused_at, off["state_since"]) if off else None, events_after_pause=events_after(paused_at))


def catch_up(k: P.Kernel, exe: Path) -> P.Kernel:
    P.consent_on()
    k.close()
    gone = P.wait_until(lambda: not P.assist_procs(), 20)
    seen = []
    proc = subprocess.Popen([str(exe), "exec", "-c", f'plugins."{P.PID}".enabled=false', "--skip-git-repo-check",
                             "-C", str(P.BED), "catch-up probe: written while no plugin process runs"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # exec reads stdin if open
    while proc.poll() is None:
        seen.extend(p["pid"] for p in P.assist_procs())
        time.sleep(0.2)
    before = len([1 for (b,) in P.store_sql("SELECT body FROM events") if "catch-up probe" in b])
    k2 = P.Kernel(exe)
    k2.start()
    k2.thread()
    found = P.wait_until(lambda: [1 for (b,) in P.store_sql("SELECT body FROM events") if "catch-up probe" in b], 30)
    record("catch_up", no_plugin_process_after_close=bool(gone), plugin_processes_during_exec=sorted(set(seen)),
           exec_exit=proc.returncode, recorded_before_reload=before, recorded_after_reload=len(found or []))
    return k2


def archive(k: P.Kernel) -> None:
    P.consent_on()
    t_full = k.thread()
    k.turn(t_full, "archive probe: fully collected before archiving")
    time.sleep(5)
    try:
        k.s.request("thread/archive", {"threadId": t_full})
        err1 = None
    except Exception as exc:  # noqa: BLE001
        err1 = str(exc)
    # content written while the collector cannot run (plugin disabled), then archived: an unreadable tail
    t_part = k.thread()
    k.turn(t_part, "archive probe: first turn")
    time.sleep(5)
    k.s.request("config/value/write", {"keyPath": f"plugins.{P.PID}.enabled", "value": False, "mergeStrategy": "replace"})
    wait_state("plugin_disabled", 10)
    k.turn(t_part, "archive probe: tail written while not collecting")
    try:
        k.s.request("thread/archive", {"threadId": t_part})
        err2 = None
    except Exception as exc:  # noqa: BLE001
        err2 = str(exc)
    k.s.request("config/value/write", {"keyPath": f"plugins.{P.PID}.enabled", "value": True, "mergeStrategy": "replace"})
    time.sleep(8)
    zsts = sorted(p.name for p in (P.HOME / "archived_sessions").glob("*")) if (P.HOME / "archived_sessions").exists() else []
    states = P.store_sql("SELECT name, state, offset FROM files")
    issues = P.store_sql("SELECT code, detail, count FROM issues WHERE category = 'missing'")
    record("archive", archive_errors=[err1, err2], archived_files=zsts, file_states=states, missing_issues=issues,
           threads={"full": t_full, "partial": t_part})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--latency-runs", type=int, default=5)
    ap.add_argument("--idle", type=int, default=60)
    ap.add_argument("--long-idle", type=int, default=0)
    args = ap.parse_args()
    exe, version = P.kernel.find_app_kernel()
    RESULTS["kernel"] = {"path": str(exe), "version": version}
    mock = subprocess.Popen([sys.executable, str(REPO / "scripts" / "mock_model_server.py"), "--port", "18790", "--tool-call"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    k = None
    try:
        P.OUT.mkdir(parents=True, exist_ok=True)
        P.prepare(exe)
        os.environ.update(ASSIST_COLLECT_MS="2000", ASSIST_NO_ORPHAN_EXIT="0", ASSIST_HEARTBEAT_SEC="30")
        k = P.Kernel(exe)
        k.start()
        threads = multi_session(k, args.sessions)
        stop_latency(k, threads, args.latency_runs, args.idle)
        if args.long_idle:
            long_idle(k, args.long_idle)
        k = catch_up(k, exe)
        archive(k)
        record("final_coverage", **coverage())
        k.close()
        record("after_close", processes=P.assist_procs())
    finally:
        if k:
            k.close()
        mock.kill()
        for p in P.assist_procs():
            subprocess.run(["taskkill", "/F", "/PID", str(p["pid"])], capture_output=True)
        if (P.DATA / "lifecycle.jsonl").exists():
            (OUT / "lifecycle.jsonl").write_bytes((P.DATA / "lifecycle.jsonl").read_bytes())
    print(f"results: {OUT / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
