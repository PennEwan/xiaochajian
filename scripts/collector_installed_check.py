"""Post-install verification: is the collector there, and is data actually reaching the backend?

Run it after installing `CodexCompanion-Setup-*.exe`, with the mock backend running:

    python scripts\\collector_installed_check.py
    python scripts\\collector_installed_check.py --root build\\stage-1.5.3\\marketplace\\plugins\\codex-companion

It reads only: the installed plugin tree, `%LOCALAPPDATA%\\CodexCompanion`, the Task Scheduler and
the backend's `_mock` views. Nothing is written except the collector's own status file (which the
collector rewrites itself anyway), and the task is never created or deleted here.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((ok, label, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    return ok


def info(label: str, detail: str) -> None:
    print(f"[ info ] {label}: {detail}")


def http_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def default_install_root() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Programs" / "CodexCompanion"


def plugin_root(install: Path) -> Path | None:
    candidates = [install / "marketplace" / "plugins" / "codex-companion",
                  install / "lib" / ".."]  # a staged tree: ...\plugins\codex-companion
    for cand in candidates:
        if (cand / "lib" / "collector").is_dir():
            return cand.resolve()
    return None


def data_root(plugin: Path) -> Path:
    """The collector's data directory: the product's, not a per-plugin one."""
    override = os.environ.get("COMPANION_DATA_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "CodexCompanion"


def python_for(plugin: Path) -> str:
    runtime = plugin / "runtime" / "python.exe"
    return str(runtime) if runtime.exists() else sys.executable


def env_for(plugin: Path, data: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin / "lib"), str(plugin.parent.parent / "codex-task-recorder" / "lib"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["COMPANION_DATA_DIR"] = str(data)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(plugin: Path, data: Path, *args: str) -> tuple[int, str, str]:
    cmd = [python_for(plugin), "-m", "collector", "--data-dir", str(data), *args]
    proc = subprocess.run(cmd, cwd=str(Path(os.environ.get("TEMP", "."))), env=env_for(plugin, data),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def task_state() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "not windows"
    proc = subprocess.run(["schtasks", "/query", "/tn", "CodexCompanion\\Collector", "/fo", "list"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return False, "not registered"
    line = next((ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("Status")), "")
    return True, line or "registered"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="plugin root (default: the installed product)")
    parser.add_argument("--base-url", help="override the backend address")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    install = Path(args.root).resolve() if args.root else default_install_root()
    plugin = install if args.root else plugin_root(install)
    if not plugin:
        print(f"找不到装好的插件：{install}\\marketplace\\plugins\\codex-companion")
        print("先安装 CodexCompanion-Setup-*.exe，或用 --root 指向 build\\stage-* 里的插件目录。")
        return 2
    data = data_root(plugin)
    base_url = args.base_url

    print(f"插件目录: {plugin}")
    print(f"数据目录: {data}\n")

    # 1. files that the installer must have put there
    for rel in ("lib/collector/run.py", "lib/csr/reader.py", "bin/collector.cmd", ".mcp.json"):
        check((plugin / rel).exists(), f"安装产物存在 {rel}")
    product = read_json(plugin / "product.json")
    check(bool(product.get("plugin_id")), "product.json 可读", product.get("plugin_id") or "missing")
    base_url = base_url or product.get("sync_base_url")
    info("后台地址", base_url or "（未配置 → 只存本机，不上传）")

    # 2. the contract record
    contract = read_json(data / "contract.json")
    check(contract.get("terms_version") is not None, "合同已留痕 terms_version",
          str(contract.get("terms_version")))
    check(contract.get("terms_hash", "").startswith("sha256:"), "条款哈希已记录")
    check(not contract.get("terminated_at"), "合同未终止", str(contract.get("terminated_at") or "—"))
    info("生效时间", str(contract.get("effective_at") or "—"))

    # 3. the collector's own config (or the product.json fallback `config.load` uses)
    cfg = read_json(data / "collector-config.json")
    configured = cfg.get("base_url") or product.get("sync_base_url")
    source = "collector-config.json" if cfg.get("base_url") else "product.json（回退）"
    check(bool(configured), "后台地址已配置", f"{configured or 'missing'} ← {source}")
    info("采集档位", str(cfg.get("tiers") or ["L0", "L1", "L2", "L3"]))

    # 4. the logon task (this is what survives the plugin). A staged tree has no task by design.
    ok, detail = task_state()
    if args.root:
        info("登录计划任务", ("已注册 — " if ok else "未注册（--root 测试模式，安装后才会有）"))
    else:
        check(ok, "登录计划任务已注册（CodexCompanion\\Collector）", detail)

    # 5. live status
    code, out, err = run(plugin, data, "status", "--json")
    status = {}
    try:
        status = json.loads(out)
    except ValueError:
        check(False, "collector status 可执行", (err or out)[-200:])
    if status:
        info("运行中", str(status.get("collector_running")))
        info("正在记录", str(status.get("recording")))
        info("gate", str(status.get("gate")))
        info("service_state", str(status.get("service_state")))
        reader = status.get("reader") or {}
        spool = status.get("spool") or {}
        info("已采集事件", str(reader.get("events")))
        info("待上传", str(spool.get("records")))
        check(status.get("gate") in ("active", "grace", "paused", "contract_terminated"),
              "gate 有明确结论", str(status.get("gate")))

    # 6. the backend
    if base_url:
        stats = http_json(base_url.rstrip("/") + "/_mock/stats")
        if stats is None:
            check(False, "后台可达", base_url)
        else:
            check(True, "后台可达", base_url)
            device_events = stats.get("events") or {}
            total = sum(device_events.values()) if isinstance(device_events, dict) else 0
            check(total > 0, "后台已收到事件", f"{total} 条")
            events = (http_json(base_url.rstrip("/") + "/_mock/events") or {}).get("events") or []
            tiers = sorted({e.get("tier") for e in events if isinstance(e, dict)})
            schemas = sorted({e.get("schema") for e in events if isinstance(e, dict)})
            info("收到的档位", ", ".join(t for t in tiers if t) or "无")
            info("事件 schema", ", ".join(s for s in schemas if s) or "无")
            if events:
                check({"L0"} <= set(tiers), "元数据（L0）已到达")
                check("comp.session/v1" in schemas, "会话遥测已到达")
                content = [e for e in events if e.get("tier") in ("L2", "L3")]
                check(bool(content), "正文（L2/L3）已到达", f"{len(content)} 条")
            beats = (http_json(base_url.rstrip("/") + "/_mock/heartbeats") or {}).get("heartbeats") or {}
            info("心跳", f"{len(beats)} 台设备")
            states = (http_json(base_url.rstrip("/") + "/_mock/service-state") or {}).get("service_state") or {}
            if isinstance(states, dict):
                for dev, rec in list(states.items())[:3]:
                    info("服务状态", f"{dev}: {rec.get('state')}")

    failed = [label for ok, label, _ in RESULTS if not ok]
    print()
    if args.json:
        print(json.dumps({"ok": not failed, "failed": failed,
                          "results": [{"ok": o, "label": lbl, "detail": d} for o, lbl, d in RESULTS]},
                         ensure_ascii=False, indent=2))
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过")
    if failed:
        print("未通过：" + "；".join(failed))
        return 1
    print("ALL PASS — 采集器已装好，并且数据正在到达后台")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
