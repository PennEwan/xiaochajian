"""Coverage, duplicate and missing rates of the installed plugin against the real Codex sessions (read-only).

Expected events come from the session reader's parser (tools/session-reader, an independent Python
implementation) over every rollout under ~/.codex/sessions and archived_sessions, kept only when the
event time falls in one of the plugin's consent periods and the session's folder was in scope then.
Only counts and event keys are printed — never content.

    python scripts\\native_app_coverage.py --scope "1=all" --scope "2-=D:\\ctr-testbed"
      (--scope <period ids>=<all | folder;folder>; consent scope is not versioned per period in the store)
Results: .testdata/native-desktop/coverage.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools" / "session-reader"))
sys.path.insert(0, str(REPO / "plugins" / "codex-task-recorder" / "lib"))
HOME = Path(os.environ["USERPROFILE"]) / ".codex"
DATA = HOME / "plugins" / "data" / "codex-native-assist-ctr-local"
OUT = REPO / ".testdata" / "native-desktop" / "coverage.json"


def parse_scopes(specs: list[str]) -> list[tuple[int, int, list[str] | None]]:
    out = []
    for spec in specs:
        ids, _, what = spec.partition("=")
        lo, _, hi = ids.partition("-")
        folders = None if what == "all" else [f.rstrip("\\").lower() for f in what.split(";")]
        out.append((int(lo), int(hi) if hi else (int(lo) if "-" not in ids else 10**9), folders))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", action="append", default=[])
    args = ap.parse_args()
    from csr.parser import Context, parse_line
    scopes = parse_scopes(args.scope)
    db = sqlite3.connect(DATA / "assist.sqlite3")
    periods = db.execute("SELECT id, started_at, ended_at FROM consent_periods ORDER BY id").fetchall()
    stored = {k: (kind, sid) for k, kind, sid in db.execute("SELECT event_key, kind, session_id FROM events")}
    sem = Counter()
    for sid, kind, item, call in db.execute("SELECT session_id, kind, json_extract(body,'$.item_id'), call_id FROM events"):
        if kind in ("user_prompt", "assistant_message", "reasoning_summary", "tool_execution"):
            sem[(sid, kind, item)] += 1
        elif kind in ("tool_call", "tool_result"):
            sem[(sid, kind, call)] += 1
    issues = db.execute("SELECT category, code, SUM(count) FROM issues GROUP BY 1, 2").fetchall()
    unrec = db.execute("SELECT category, SUM(count) FROM skipped WHERE category LIKE 'unrecognized:%' GROUP BY 1").fetchall()
    db.close()

    def allowed(ts: str, cwd: str | None) -> bool:
        for pid, a, b in periods:
            if a <= ts and (b is None or ts <= b):
                folders = next((f for lo, hi, f in scopes if lo <= pid <= hi), None)
                if folders is None:
                    return True
                c = (cwd or "").lower().rstrip("\\") + "\\"
                return bool(cwd) and any(c.startswith(f + "\\") for f in folders)
        return False

    earliest = periods[0][1] if periods else "9999"
    expected = {}
    files = list((HOME / "sessions").rglob("rollout-*.jsonl")) + list((HOME / "archived_sessions").rglob("rollout-*.jsonl"))
    for path in files:
        if path.stat().st_mtime < 0:
            continue
        ctx = Context(session_id=path.stem[-36:])
        cwd, offset = None, 0
        with open(path, "rb") as f:
            data = f.read()
        if b'"timestamp":"' + earliest[:10].encode() not in data and earliest[:10].encode() not in data:
            continue  # nothing from the consent days in this file
        for raw in data.split(b"\n"):
            if raw:
                parsed = parse_line(raw, offset, ctx, path.name)
                for e in parsed.events:
                    if e["kind"] == "session_meta":
                        cwd = e["data"].get("cwd")
                    if e["ts"] and allowed(e["ts"], cwd):
                        expected[e["event_key"]] = e["kind"]
            offset += len(raw) + 1
    matched = [k for k in expected if k in stored and stored[k][0] == expected[k]]
    missing = [k for k in expected if k not in stored]
    extra = [k for k in stored if k not in expected]
    by_kind = Counter(expected.values())
    got = Counter(expected[k] for k in matched)
    result = {
        "consent_periods": periods, "scopes": args.scope,
        "expected_events": len(expected), "stored_events": len(stored), "matched": len(matched),
        "missing": len(missing), "extra": len(extra), "semantic_duplicates": sum(n - 1 for n in sem.values() if n > 1),
        "coverage_rate": round(len(matched) / len(expected), 4) if expected else None,
        "missing_rate": round(len(missing) / len(expected), 4) if expected else None,
        "duplicate_rate": round(sum(n - 1 for n in sem.values() if n > 1) / max(len(stored), 1), 4),
        "by_kind": {k: f"{got.get(k, 0)}/{n}" for k, n in sorted(by_kind.items())},
        "missing_keys": missing[:20], "extra_keys": extra[:20],
        "plugin_reported_issues": issues, "plugin_reported_unrecognized": unrec,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
