"""Validate the session reader against one real, user-designated Codex test session.

Read-only towards the source: the session file is only opened for reading (shared mode);
all copies live in a temporary folder that is deleted afterwards. Prints a JSON report
without any conversation content.

    set PYTHONPATH=tools\\session-reader;plugins\\codex-task-recorder\\lib
    python scripts\\csr_live_check.py --session-file %USERPROFILE%\\.codex\\sessions\\2026\\10\\28\\rollout-…-<id>.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest import mock

from csr import reader, scanner, summary
from csr.storage import Storage


def fingerprint(path: Path) -> dict:
    with reader.open_shared(path) as f:
        data = f.read()
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": hashlib.sha256(data).hexdigest()}


def bodies(storage: Storage) -> list[str]:
    return [json.dumps(e, sort_keys=True, ensure_ascii=False) for e in storage.iter_events()]


def comparable(stats: dict) -> dict:
    return {k: v for k, v in stats.items() if k not in ("files", "other_issues")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-file", required=True, type=Path)
    ap.add_argument("--seed", type=int, default=20261029)
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = ap.parse_args()
    src = args.session_file
    sid = reader.session_id_from_name(src.name)
    if not sid:
        print("not a rollout-<time>-<uuid>.jsonl file", file=sys.stderr)
        return 2
    before = fingerprint(src)
    with reader.open_shared(src) as f:
        data = f.read()
    tmp = Path(tempfile.mkdtemp(prefix="csr-live-"))
    checks: dict[str, bool] = {}
    report: dict = {"session_id": sid, "source_size": before["size"]}
    try:
        # 1. One pass straight from the Codex folder, filtered to this session only.
        direct = Storage(tmp / "direct.sqlite3")
        res = scanner.scan(direct, src.parent, sessions=[sid])
        checks["direct_scan_reads_only_the_designated_file"] = [Path(r.path).name for r in res] == [src.name]
        ref_bodies = bodies(direct)
        ref_stats = summary.session_stats(direct, sid)
        report["stats"] = {k: v for k, v in ref_stats.items() if k != "files"}
        report["coverage"] = summary.coverage(direct, [sid])
        report["coverage_markdown"] = summary.coverage_markdown(report["coverage"])
        keys = [json.loads(b)["event_key"] for b in ref_bodies]
        checks["no_duplicate_event_keys"] = len(keys) == len(set(keys))
        checks["no_parse_failures_in_real_file"] = ref_stats["parse_failures"] == 0
        direct.close()

        # 2. Replay: append the same bytes in random chunks, scanning, restarting and crashing in between.
        rng = random.Random(args.seed)
        replay_dir = tmp / "replay"
        replay_dir.mkdir()
        target = replay_dir / src.name
        target.write_bytes(b"")
        db = tmp / "replay.sqlite3"
        storage = Storage(db)
        pos = steps = restarts = crashes = partial_scans = 0
        crash_at = rng.randint(5, 15)
        while pos < len(data):
            n = rng.randint(1, 6000)
            with open(target, "ab") as f:
                f.write(data[pos:pos + n])
            pos += n
            steps += 1
            if steps == crash_at:
                with mock.patch.object(Storage, "_add_skipped", side_effect=sqlite3.OperationalError("simulated")):
                    r = scanner.ingest_file(storage, target, max_bytes=4096, batch_lines=7)
                crashes += r.status == "error"
            [r] = scanner.scan(storage, replay_dir, max_bytes=4096, batch_lines=7)
            partial_scans += r.pending > 0
            if steps % 4 == 0:
                storage.close()
                storage = Storage(db)
                restarts += 1
        replay_bodies = bodies(storage)
        replay_stats = summary.session_stats(storage, sid)
        storage.close()
        report["replay"] = {"appends": steps, "restarts": restarts, "simulated_crashes": crashes,
                            "scans_with_incomplete_last_line": partial_scans}
        checks["replay_matches_single_pass_event_by_event"] = replay_bodies == ref_bodies
        checks["replay_stats_match"] = comparable(replay_stats) == comparable(ref_stats)
        checks["replay_recorded_the_crash_and_nothing_else"] = (
            replay_stats["other_issues"] == {"store_failed": 1} and ref_stats["other_issues"] == {})
        checks["replay_saw_incomplete_lines"] = partial_scans > 0
        checks["replay_survived_simulated_crash"] = crashes == 1

        # 3. Damaged copy: one line cut in half, one garbage line, an unfinished tail.
        lines = data.split(b"\n")
        offsets = [sum(len(line) + 1 for line in lines[:i]) for i in range(len(lines))]
        event_offsets = Counter(json.loads(b)["source"]["offset"] for b in ref_bodies)
        # cut the first line past the middle that produced events, so the loss is observable
        victim = next(i for i in range(len(lines) // 2, len(lines)) if event_offsets[offsets[i]])
        lost = event_offsets[offsets[victim]]
        damaged = list(lines[:-1])
        damaged[victim] = damaged[victim][: len(damaged[victim]) // 2]
        damaged.insert(victim + 3, b"\x00\x01 not json at all")
        bad_dir = tmp / "damaged"
        bad_dir.mkdir()
        (bad_dir / src.name).write_bytes(b"\n".join(damaged) + b"\n" + b'{"timestamp": "2026-')
        bad = Storage(tmp / "damaged.sqlite3")
        [r] = scanner.scan(bad, bad_dir)
        bad_stats = summary.session_stats(bad, sid)
        bad.close()
        report["damaged"] = {"parse_failures": bad_stats["parse_failures"],
                             "ordinal_gap_records": bad_stats["missing"]["ordinal_gap_records"],
                             "events": bad_stats["events"], "events_in_intact_file": ref_stats["events"],
                             "events_from_the_cut_line": lost, "pending_bytes": r.pending}
        checks["damaged_lines_are_reported_not_fatal"] = (
            bad_stats["parse_failures"] == 2 and bad_stats["missing"]["ordinal_gap_records"] == 0
            and bad_stats["events"] == ref_stats["events"] - lost and r.pending == len(b'{"timestamp": "2026-'))
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    after = fingerprint(src)
    checks["source_file_unchanged"] = before == after
    report["checks"] = checks
    report["all_passed"] = all(checks.values())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
