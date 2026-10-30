"""Drive the real recharge window against the in-process mock backend and screenshot each step.

    set PYTHONPATH=plugins\\codex-cdk-recharge\\lib;tests
    python scripts\\live_window_demo.py [--out .testdata\\cdkr-window]

Uses the real window (worker threads), the real flow and the real `account/read` through the
App kernel; only the backend is the mock, and the shown email is masked. Data goes to a
temporary CDKR_HOME, so the user's own settings are untouched.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from cdkr import account, config, flow, window

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from cdkr_helpers import MockBackend  # noqa: E402

PS_SHOT = ("Add-Type -AssemblyName System.Drawing; $b = New-Object System.Drawing.Bitmap {w},{h}; "
           "$g = [System.Drawing.Graphics]::FromImage($b); $g.CopyFromScreen({x},{y},0,0,$b.Size); "
           "$b.Save('{path}'); $g.Dispose(); $b.Dispose()")


def masked_account() -> flow.Account:
    real = account.read_account()
    return flow.Account(real.account_id, re.sub(r"^(.).*(@.*)$", r"\1***\2", real.email), real.plan)


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=".testdata/cdkr-window")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="cdkr-demo-"))
    backend = MockBackend()
    paths = config.Paths(home)
    cfg = config.Config(base_url=backend.url, timeout=15)
    recharge = flow.Recharge(paths, cfg, masked_account)
    app = window.App(recharge, local_backend=True)
    results: list[dict] = []

    def shot(name: str) -> None:
        app.root.update()
        x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
        w, h = app.root.winfo_width(), app.root.winfo_height()
        path = out / f"{len(results):02d}-{name}.png"
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        PS_SHOT.format(x=x, y=y, w=w, h=h, path=str(path))], check=False)
        results.append({"step": name, "button": app.button["text"], "status": app.status_var.get(),
                        "cdk_box": app.cdk_var.get(), "shot": path.name, "size": [w, h],
                        "region_h": window.region_height(app.root)})

    def go_down() -> None:  # same account, backend now unreachable
        recharge.config = config.Config(base_url=f"http://127.0.0.1:{closed_port()}", timeout=5)

    steps = [
        ("first-open", None),
        ("redeem-ok", lambda: (app.cdk_var.set("TEST-OK01"), app.button.invoke())),
        ("redeem-duplicate", lambda: (app.cdk_var.set("TEST-OK01"), app.button.invoke())),
        ("redeem-invalid", lambda: (app.cdk_var.set("WRONG-CODE-123"), app.button.invoke())),
        ("redeem-expired", lambda: (app.cdk_var.set("TEST-EXPD"), app.button.invoke())),
        ("network-down", lambda: (go_down(), app.cdk_var.set("TEST-OK02"), app.button.invoke())),
    ]

    def idle() -> bool:
        return not app._busy  # the button stays off after a success: the CDK box is empty again

    def run_step(i: int = 0) -> None:
        if not idle():
            app.root.after(200, run_step, i)
            return
        if i > 0:
            shot(steps[i - 1][0])
        if i == len(steps):
            app.root.after(300, app.root.destroy)
            return
        action = steps[i][1]
        if action:
            action()
        app.root.after(400, run_step, i + 1)

    app.root.after(500, run_step, 0)
    app.run()
    backend.stop()
    log = paths.log.read_text(encoding="utf-8") if paths.log.exists() else ""
    shutil.rmtree(home, ignore_errors=True)
    (out / "results.json").write_text(json.dumps({"steps": results, "log": log}, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    for r in results:
        print(f"{r['step']:<18} [{r['button']}] {r['status']}")
    print("log lines:", len(log.splitlines()), "| CDK in log:", any(c in log for c in ("TEST-OK01", "WRONG-CODE")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
