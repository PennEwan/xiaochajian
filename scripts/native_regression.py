"""Regression baselines for the native plugin work, isolated from the other uncommitted work in this repo.

Runs the whole unit-test suite in four trees and records every test's outcome:
  B0 head      — a git worktree at HEAD (committed state only)
  B1 before    — a copy of the current working tree with this work's files removed / reverted
  B2 current   — the current working tree as is
  B3 isolated  — the branch worktree feat/native-plugin (HEAD + only this work's files)

    python scripts\\native_regression.py [--trees head,before,current,isolated] [--isolated D:\\codex-task-recorder-native]
Results: .testdata/native-regression/<tree>.json and compare.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / ".testdata" / "native-regression"
TMP = Path(os.environ.get("NAT_REGRESSION_TMP", r"D:\tmp\nat-regression"))

# This work's own files (relative to the repo root).
NATIVE_PATHS = ["native", "plugins/codex-native-assist", "tests/test_native_assist.py", "tests/native_rollout.py",
                "scripts/appui.py", "docs/native-plugin-feasibility.md", "docs/native-assist-manual-test.md",
                "docs/native-p4-acceptance-report.md"]
NATIVE_GLOBS = ["scripts/native_*.py"]

RUNNER = r'''
import json, sys, time, unittest
class R(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.out = {}
    def addSuccess(self, t): super().addSuccess(t); self.out[t.id()] = "pass"
    def addFailure(self, t, e): super().addFailure(t, e); self.out[t.id()] = "fail"
    def addError(self, t, e): super().addError(t, e); self.out[t.id()] = "error"
    def addSkip(self, t, r): super().addSkip(t, r); self.out[t.id()] = "skip"
    def addExpectedFailure(self, t, e): super().addExpectedFailure(t, e); self.out[t.id()] = "xfail"
    def addUnexpectedSuccess(self, t): super().addUnexpectedSuccess(t); self.out[t.id()] = "xpass"
start = time.time()
suite = unittest.defaultTestLoader.discover("tests", top_level_dir="tests")
runner = unittest.TextTestRunner(resultclass=R, verbosity=0, stream=open("nul", "w"))
res = runner.run(suite)
fails = {t.id(): tb[-1500:] for t, tb in res.failures + res.errors}
json.dump({"seconds": round(time.time() - start, 1), "ran": res.testsRun, "outcomes": res.out, "tracebacks": fails},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
'''


def native_files(root: Path) -> list[Path]:
    out = []
    for p in NATIVE_PATHS:
        if (root / p).exists():
            out.append(root / p)
    for g in NATIVE_GLOBS:
        out.extend(sorted(root.glob(g)))
    return out


def run_suite(tree: Path, name: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASSIST_")}
    libs = [tree / "plugins" / "codex-task-recorder" / "lib", tree / "plugins" / "codex-cdk-recharge" / "lib",
            tree / "plugins" / "codex-companion" / "lib", tree / "tools" / "session-reader", tree / "tests"]
    env.update(PYTHONPATH=os.pathsep.join(str(p) for p in libs if p.exists()), PYTHONIOENCODING="utf-8")
    out = OUT / f"{name}.json"
    script = TMP / "runner.py"
    script.write_text(RUNNER, encoding="utf-8")
    t0 = time.time()
    subprocess.run([sys.executable, "-W", "ignore", str(script), str(out)], cwd=tree, env=env, timeout=3600,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = json.loads(out.read_text(encoding="utf-8"))
    counts = {}
    for v in data["outcomes"].values():
        counts[v] = counts.get(v, 0) + 1
    data["tree"], data["path"], data["counts"] = name, str(tree), counts
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{name:9} {tree}  ran {data['ran']}  {counts}  ({round(time.time() - t0)} s)", flush=True)
    return data


def copy_tree(dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "build", "dist", ".testdata", "__pycache__", "*.pyc"))


def make_before() -> Path:
    """The working tree as it was before this work: its files removed, its edits to shared files reverted."""
    dst = TMP / "before"
    copy_tree(dst)
    for p in native_files(dst):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    market = dst / ".agents" / "plugins" / "marketplace.json"
    data = json.loads(market.read_text(encoding="utf-8"))
    data["plugins"] = [p for p in data["plugins"] if p["name"] != "codex-native-assist"]
    market.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dst


def make_head() -> Path:
    dst = TMP / "head"
    if dst.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(dst)], cwd=REPO, capture_output=True)
        shutil.rmtree(dst, ignore_errors=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(dst), "HEAD"], cwd=REPO, check=True, capture_output=True)
    return dst


def compare(results: dict) -> dict:
    names = list(results)
    every = sorted(set().union(*(r["outcomes"] for r in results.values())))
    rows = {}
    for t in every:
        outs = {n: results[n]["outcomes"].get(t, "absent") for n in names}
        if len(set(o for o in outs.values() if o != "absent")) > 1 or any(o in ("fail", "error") for o in outs.values()):
            rows[t] = outs
    return {"trees": {n: {"path": r["path"], "ran": r["ran"], "counts": r["counts"]} for n, r in results.items()},
            "not_all_passing": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trees", default="head,before,current,isolated")
    ap.add_argument("--isolated", default=r"D:\codex-task-recorder-native")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    trees = {"head": make_head, "before": make_before, "current": lambda: REPO, "isolated": lambda: Path(args.isolated)}
    results = {}
    for name in args.trees.split(","):
        results[name] = run_suite(trees[name](), name)
    cmp_ = compare(results)
    (OUT / "compare.json").write_text(json.dumps(cmp_, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(cmp_, ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
