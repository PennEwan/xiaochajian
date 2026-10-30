"""Task 3 in the real Codex App: several sessions at once, recorded by the installed plugin.

Drives the App UI (scripts/appui.py): N new chats, the first `--in-project` in the current project and the
rest with 「不在项目中工作」, one short prompt each, sent back to back so the turns overlap. Then reads the
plugin's store (Codex's plugin data folder) with sqlite3 and the process table.

    python scripts\\native_app_multisession.py [--sessions 5] [--in-project 3] [--tag C]
Results: .testdata/native-desktop/multisession.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import appui  # noqa: E402

DATA = Path(os.environ["USERPROFILE"]) / ".codex" / "plugins" / "data" / "codex-native-assist-ctr-local"
CACHE = str(Path(os.environ["USERPROFILE"]) / ".codex" / "plugins").lower()  # cache\… or data\…\bin (launcher copy)
OUT = REPO / ".testdata" / "native-desktop" / "multisession.json"


def q(sql: str, args: tuple = ()) -> list:
    c = sqlite3.connect(DATA / "assist.sqlite3", timeout=10)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def procs() -> list[dict]:
    ps = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'assist%.exe'\" | Select-Object ProcessId,ParentProcessId,"
          "ExecutablePath,CommandLine,PrivatePageCount,WorkingSetSize | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
    rows = json.loads(out) if out else []
    rows = rows if isinstance(rows, list) else [rows]
    return [{"pid": r["ProcessId"], "mode": (r.get("CommandLine") or "").split()[-1],
             "private_mb": round(int(r["PrivatePageCount"]) / 2**20, 1), "working_set_mb": round(int(r["WorkingSetSize"]) / 2**20, 1)}
            for r in rows if str(r.get("ExecutablePath") or "").lower().startswith(CACHE)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=5)
    ap.add_argument("--in-project", type=int, default=3)
    ap.add_argument("--tag", default="C")
    args = ap.parse_args()
    w = appui.app_window()
    h = w["hwnd"]
    before = {r[0] for r in q("SELECT DISTINCT session_id FROM events")}
    p0 = procs()
    sent = []
    for i in range(1, args.sessions + 1):
        appui.bring_to_front(h)
        time.sleep(0.3)
        appui.click(h, 126, 301)  # 新聊天
        time.sleep(1.5)
        l, t, r, b = appui.rect(h)
        if i > args.in_project:
            appui.click(h, l + 436, t + 684)  # project chip
            time.sleep(1.0)
            appui.click(h, l + 466, t + 649)  # 不在项目中工作
            time.sleep(1.0)
        appui.click(h, l + 700, t + 727)
        time.sleep(0.4)
        text = f"并发测试 {args.tag}{i}：只回复 ok"
        appui.type_text(h, text)
        appui.press(h, appui.VK_RETURN)
        sent.append({"i": i, "text": text, "in_project": i <= args.in_project, "at": time.strftime("%H:%M:%S")})
        time.sleep(2)
    peak = []
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        ps = procs()
        if len(ps) > len(peak):
            peak = ps
        time.sleep(1)
    time.sleep(5)
    rows = q("SELECT session_id, kind, json_extract(body, '$.data.text'), json_extract(body, '$.item_id'), "
             "json_extract(body, '$.call_id'), event_key FROM events")
    prompts = {}
    for sid, kind, text, *_ in rows:
        if kind == "user_prompt" and text and f"并发测试 {args.tag}" in text:
            prompts.setdefault(text.strip(), []).append(sid)
    sem = Counter()
    for sid, kind, _, item, call, _k in rows:
        if kind in ("user_prompt", "assistant_message", "reasoning_summary", "tool_execution"):
            sem[(sid, kind, item)] += 1
        elif kind in ("tool_call", "tool_result"):
            sem[(sid, kind, call)] += 1
    keys = [r[5] for r in rows]
    new_sessions = {r[0] for r in rows} - before
    cwd = dict(q("SELECT session_id, json_extract(body, '$.data.cwd') FROM events WHERE kind = 'session_meta'"))
    leaders = sorted({json.loads(l)["pid"] for l in (DATA / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
                      if '"collector_leader"' in l})
    result = {
        "sent": sent,
        "processes_before": p0, "processes_peak": peak,
        "peak_mcp_instances": sum(1 for p in peak if p["mode"] == "mcp"),
        "peak_private_mb_total": round(sum(p["private_mb"] for p in peak), 1),
        "recorded_prompts": {t: len(s) for t, s in prompts.items()},
        "expected_recorded": [s["text"] for s in sent if s["in_project"]],
        "expected_not_recorded": [s["text"] for s in sent if not s["in_project"]],
        "scope_ok": sorted(prompts) == sorted(s["text"] for s in sent if s["in_project"]),
        "duplicate_prompts": {t: len(s) for t, s in prompts.items() if len(s) != 1},
        "rows": len(keys), "distinct_keys": len(set(keys)),
        "semantic_duplicates": sum(n - 1 for n in sem.values() if n > 1),
        "new_sessions_recorded": {s: cwd.get(s) for s in new_sessions},
        "collector": q("SELECT state, last_error, leader_pid FROM collector")[0],
        "collector_leaders_ever": leaders,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
