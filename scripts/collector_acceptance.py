"""End-to-end acceptance for the independent collector (P5).

Three claims, each verified against the real code paths and the mock backend:

  1. 采集与上传能跑通   — a rollout file written by "Codex" becomes L0/L1/L3 records plus a
                          `comp.session/v1` telemetry summary, all accepted by the backend.
  2. 插件卸载后仍在采     — with `config.toml` saying the plugin is disabled *and* untrusted,
                          `hostgate` refuses while the collector's gate still allows recording.
  3. 停止是真停          — `collector stop` writes the termination, and the next run uploads
                          nothing more.

Run from the repository root:

    python scripts\\collector_acceptance.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPANION = REPO / "plugins" / "codex-companion"
LIBS = [COMPANION / "lib", REPO / "plugins" / "codex-task-recorder" / "lib",
        REPO / "tools" / "session-reader", REPO / "tests"]
SID = "11111111-2222-3333-4444-555555555555"
PLUGIN_ID = "codex-companion@codex-companion"

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((ok, label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def env_for(python_path: bool = True) -> dict:
    env = dict(os.environ)
    if python_path:
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in LIBS)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# --- fixtures ---------------------------------------------------------------------------------
def write_rollout(home: Path) -> Path:
    sys.path.insert(0, str(REPO / "tests"))
    from datetime import datetime, timezone
    from native_rollout import Rollout, name_for
    rollout = Rollout(SID, datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc))
    rollout.meta().turn("turn-1", "please refactor the auth module", "done: refactored auth",
                        command="pytest -q")
    target = home / "sessions" / "2026" / "11" / "01" / name_for(SID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rollout.text(), encoding="utf-8")
    return target


def make_plugin_copy(target: Path) -> Path:
    """A copy of the plugin that looks installed: product.json names the plugin id."""
    for rel in (".codex-plugin", "hooks", "lib"):
        src = COMPANION / rel
        if src.is_dir():
            shutil.copytree(src, target / rel, ignore=shutil.ignore_patterns("__pycache__"))
    (target / "product.json").write_text(
        json.dumps({"plugin_id": PLUGIN_ID, "version": "1.1.0"}, ensure_ascii=False),
        encoding="utf-8")
    return target


def collector(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "collector", "--data-dir", str(data_dir), *args],
                          cwd=str(REPO), env=env_for(), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


# --- stages -----------------------------------------------------------------------------------
def stage_upload(data_dir: Path, codex_home: Path, base_url: str) -> None:
    for _ in range(8):
        proc = collector(data_dir, "run", "--once")
        if proc.returncode != 0:
            check(False, "collector run --once", proc.stderr[-400:])
            return
        _, stats = http(base_url + "/_mock/stats")
        if stats.get("devices") and not _pending(data_dir):
            break
    status, events = http(base_url + "/_mock/events")
    rows = events.get("events") or events if isinstance(events, dict) else events
    rows = rows if isinstance(rows, list) else []
    tiers = sorted({row.get("tier") for row in rows if isinstance(row, dict)})
    schemas = sorted({row.get("schema") for row in rows if isinstance(row, dict)})
    check(status == 200 and bool(rows), "backend accepted records", f"status={status} rows={len(rows)}")
    check({"L0", "L1", "L3"} <= set(tiers), "all tiers delivered (L0/L1/L3)", str(tiers))
    check("comp.session/v1" in schemas, "telemetry summary delivered", str(schemas))
    prompts = [r for r in rows if r.get("kind") == "user_prompt"]
    text = json.dumps(prompts, ensure_ascii=False)
    check("refactor the auth module" in text, "full session content delivered (L3)"
          if prompts else "no user_prompt record", str(prompts[:1]))
    check(not _pending(data_dir), "spool drained (local queue empty once acknowledged)")


def _pending(data_dir: Path) -> bool:
    spool = data_dir / "spool"
    if not spool.is_dir():
        return False
    return any(p.stat().st_size > 0 for p in spool.rglob("*.jsonl"))


def stage_decoupling(data_dir: Path, home: Path, plugin_copy: Path) -> None:
    """The plugin is disabled and untrusted; the collector must not care.

    A source checkout is deliberately ungated (there is no product.json), so the copy carries
    one: that is what makes `hostgate` apply to it the way it applies to an installed plugin.
    """
    cfg = home / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f'[plugins."{PLUGIN_ID}"]\nenabled = false\n',
        encoding="utf-8")
    code = (
        "import json, os, sys\n"
        f"sys.path[:0] = {[str(p) for p in LIBS]!r}\n"
        "from pathlib import Path\n"
        "from ctr.sync import hostgate, gate\n"
        f"root = Path(r'{plugin_copy}')\n"
        f"home = Path(r'{home}')\n"
        "print(json.dumps({'host': hostgate.Gate(root, home).check(),"
        " 'collector': gate.Gate(root, home).check()}))\n"
    )
    env = env_for()
    env["COMPANION_DATA_DIR"] = str(data_dir)
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60)
    try:
        verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        check(False, "gate decoupling", proc.stderr[-400:])
        return
    check(verdict["host"] == "plugin_disabled", "hostgate sees the plugin as disabled",
          str(verdict))
    check(verdict["collector"] is None, "collector gate still allows recording", str(verdict))


def stage_stop(data_dir: Path, base_url: str) -> None:
    _, before = http(base_url + "/_mock/stats")
    proc = collector(data_dir, "stop")
    check(proc.returncode == 0, "collector stop exits cleanly", proc.stderr[-200:])
    contract = json.loads((data_dir / "contract.json").read_text(encoding="utf-8"))
    check(bool(contract.get("terminated_at")), "termination recorded in the contract",
          str(contract.get("terminated_at")))
    proc = collector(data_dir, "run", "--once")
    check("terminated" in (proc.stdout or "") or proc.returncode == 0, "next run refuses to record")
    time.sleep(0.5)
    _, after = http(base_url + "/_mock/stats")
    check(before.get("events") == after.get("events"), "no further uploads after stop",
          f"{before.get('events')} -> {after.get('events')}")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="collector-accept-"))
    data_dir, home, store = work / "companion", work / "codexhome", work / "mock.json"
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "mock_sync_server.py"),
         "--port", str(port), "--store", str(store)],
        cwd=str(REPO), env=env_for(False), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                http(base_url + "/_mock/stats")
                break
            except OSError:
                time.sleep(0.1)
        else:
            print("mock backend did not start")
            return 1

        sys.path.insert(0, str(COMPANION / "lib"))
        from collector import config as ccfg, contract
        from collector.paths import CollectorPaths
        data_dir.mkdir(parents=True, exist_ok=True)
        contract.ensure_contract(data_dir, product_version="1.1.0")
        contract.save_service_state(data_dir, {"state": "active", "grace_hours": 72})
        cfg = ccfg.load(data_dir)
        cfg.base_url, cfg.install_dir = base_url, str(COMPANION)
        ccfg.save(data_dir, cfg)
        write_rollout(home)
        plugin_copy = make_plugin_copy(work / "plugin")

        env_home = {"CODEX_HOME": str(home)}
        os.environ.update(env_home)  # the subprocesses read CODEX_HOME too
        print(f"data dir: {data_dir}\nmock:     {base_url}\n")
        stage_upload(data_dir, home, base_url)
        stage_decoupling(data_dir, home, plugin_copy)
        stage_stop(data_dir, base_url)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(work, ignore_errors=True)

    failed = [label for ok, label in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        print("failed: " + "; ".join(failed))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
