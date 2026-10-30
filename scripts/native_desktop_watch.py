"""Watch the plugin's MCP server under the real Codex desktop App (task 1, desktop part).

Prints a timeline of: assist.exe processes (pid, parent, start time), ChatGPT.exe/app-server
presence, and new lines from %LOCALAPPDATA%\\CodexNativeAssist\\lifecycle.jsonl. Read-only: it
never starts, stops or talks to Codex.

    python scripts\\native_desktop_watch.py [--seconds 300] [--every 5] [--out .testdata\\native-desktop\\watch.jsonl]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

LIFECYCLE = Path(os.environ["USERPROFILE"]) / ".codex" / "plugins" / "data" / "codex-native-assist-ctr-local" / "lifecycle.jsonl"
REAL_CACHE = str(Path(os.environ["USERPROFILE"]) / ".codex" / "plugins").lower()  # cache\… or data\…\bin (launcher copy)
PS = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'assist*.exe' -or $_.Name -in 'ChatGPT.exe','codex.exe' } | "
      "Select-Object ProcessId,ParentProcessId,Name,CreationDate,CommandLine,ExecutablePath,PrivatePageCount,WorkingSetSize | "
      "ConvertTo-Json -Compress")


def snapshot() -> dict:
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", PS], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout.strip()
    rows = json.loads(out) if out else []
    rows = rows if isinstance(rows, list) else [rows]
    by_pid = {r["ProcessId"]: r for r in rows}
    assist = [{"pid": r["ProcessId"], "ppid": r["ParentProcessId"],
               "parent": (by_pid.get(r["ParentProcessId"]) or {}).get("Name"),
               "mode": (r.get("CommandLine") or "").split()[-1] if r.get("CommandLine") else None,
               "private_mb": round(int(r.get("PrivatePageCount") or 0) / 2**20, 1)}
              for r in rows if r["Name"].lower().startswith("assist")
              and str(r.get("ExecutablePath") or "").lower().startswith(REAL_CACHE)]  # the App's install only
    app_servers = [r["ProcessId"] for r in rows if r["Name"] == "codex.exe" and "app-server" in (r.get("CommandLine") or "")]
    return {"app_running": any(r["Name"] == "ChatGPT.exe" for r in rows), "app_servers": app_servers, "assist": assist}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--every", type=float, default=5)
    ap.add_argument("--out", default=r".testdata\native-desktop\watch.jsonl")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = len(LIFECYCLE.read_text(encoding="utf-8").splitlines()) if LIFECYCLE.exists() else 0
    last = None
    end = time.monotonic() + args.seconds
    with out.open("a", encoding="utf-8") as f:
        while time.monotonic() < end:
            snap = snapshot()
            lines = LIFECYCLE.read_text(encoding="utf-8").splitlines() if LIFECYCLE.exists() else []
            new = [json.loads(x) for x in lines[seen:] if x.strip()]
            seen = len(lines)
            events = [{k: e.get(k) for k in ("ts", "pid", "event", "reason", "tool", "client", "ppid", "parent", "in_job", "state", "by", "at", "period_end", "period_start", "ms", "is_error")
                       if e.get(k) is not None} for e in new if e.get("event") != "heartbeat"]
            beats = sorted({e["pid"] for e in new if e.get("event") == "heartbeat"})
            key = json.dumps({**snap, "assist": [{k: v for k, v in a.items() if k != "private_mb"} for a in snap["assist"]]},
                             sort_keys=True)
            if key != last or events:
                row = {"t": time.strftime("%H:%M:%S"), **snap, "events": events, "heartbeat_pids": beats}
                print(json.dumps(row, ensure_ascii=False), flush=True)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                last = key
            time.sleep(args.every)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
