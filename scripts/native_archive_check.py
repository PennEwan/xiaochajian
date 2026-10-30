"""Check the plugin against the user's real archived sessions (read-only; prints sizes and type counts only).

1. For each archived rollout-*.jsonl.zst, compare the uncompressed size assist.exe reads from the zstd
   frame header with the real decompressed length (Codex's bundled Node has zlib.zstdDecompressSync).
2. Decompress the oldest archive (an older Codex version) into a throwaway CODEX_HOME and let the
   collector process it: old record formats must be reported as unrecognized / missing, never guessed.

    python scripts\\native_archive_check.py [--limit 40]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXE = REPO / "plugins" / "codex-native-assist" / "bin" / "assist.exe"
ARCHIVE = Path(os.environ["USERPROFILE"]) / ".codex" / "archived_sessions"
OUT = REPO / ".testdata" / "native-acceptance" / "archive-check.json"
NODE_JS = ("const z=require('zlib'),fs=require('fs');const out={};"
           "for(const f of JSON.parse(fs.readFileSync(0,'utf8'))){try{out[f]=z.zstdDecompressSync(fs.readFileSync(f)).length}"
           "catch(e){out[f]='error: '+e.message}}process.stdout.write(JSON.stringify(out))")


def node() -> Path:
    base = Path(os.environ["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "runtimes" / "cua_node"
    return next(base.glob("*/bin/node.exe"))


def decompress(src: Path, dst: Path) -> None:
    js = ("const z=require('zlib'),fs=require('fs');"
          f"fs.writeFileSync({json.dumps(str(dst))}, z.zstdDecompressSync(fs.readFileSync({json.dumps(str(src))})))")
    subprocess.run([str(node()), "-e", js], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()
    files = sorted(ARCHIVE.glob("rollout-*.jsonl.zst"))[: args.limit]
    real = json.loads(subprocess.run([str(node()), "-e", NODE_JS], input=json.dumps([str(f) for f in files]),
                                     capture_output=True, text=True, encoding="utf-8").stdout)
    rows, mismatched = [], 0
    for f in files:
        header = json.loads(subprocess.run([str(EXE), "zst-size", str(f)], capture_output=True, encoding="utf-8").stdout)["content_size"]
        ok = header == real[str(f)]
        mismatched += not ok
        rows.append({"file": f.name, "header_size": header, "decompressed": real[str(f)], "match": ok})
    result = {"archives_checked": len(rows), "mismatched": mismatched, "samples": rows[:5]}

    # The oldest archive, through the collector in a throwaway home.
    oldest = files[0]
    tmp = Path(tempfile.mkdtemp(prefix="nat-old-"))
    try:
        home, data = tmp / "home", tmp / "data"
        day = home / "sessions" / "2026" / "07" / "17"
        day.mkdir(parents=True)
        data.mkdir()
        target = day / oldest.name[:-4]
        decompress(oldest, target)
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        types = {}
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                types["<bad json>"] = types.get("<bad json>", 0) + 1
                continue
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            key = rec.get("type", "?") + ("/" + p["type"] if isinstance(p.get("type"), str) else "")
            types[key] = types.get(key, 0) + 1
        env = {**os.environ, "ASSIST_DATA_DIR": str(data), "CODEX_HOME": str(home), "ASSIST_RECORDING_DEFAULT": "off"}
        subprocess.run([str(EXE), "status"], env=env, capture_output=True)
        db = sqlite3.connect(data / "assist.sqlite3")
        db.execute("INSERT INTO consent_periods(started_at) VALUES ('2000-01-01T00:00:00.000Z')")
        db.execute("UPDATE consent SET enabled = 1, version = version + 1 WHERE id = 1")
        db.commit()
        db.close()
        collected = json.loads(subprocess.run([str(EXE), "collect-once"], env=env, capture_output=True, encoding="utf-8").stdout)
        status = json.loads(subprocess.run([str(EXE), "status"], env=env, capture_output=True, encoding="utf-8").stdout)["collector"]
        db = sqlite3.connect(data / "assist.sqlite3")
        version = db.execute("SELECT json_extract(body, '$.data.cli_version') FROM events WHERE kind = 'session_meta'").fetchone()
        db.close()
        result["old_session"] = {"file": oldest.name, "codex_version": version[0] if version else None, "lines": len(lines),
                                 "record_types": types, "collect_once": collected, "events_by_kind": status["by_kind"],
                                 "reported_unrecognized": status["unrecognized"], "reported_missing": status["missing"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if mismatched == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
