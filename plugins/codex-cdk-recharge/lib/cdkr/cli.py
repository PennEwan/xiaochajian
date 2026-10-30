"""Command line behind 安装充值插件 / 充值 / 卸载充值插件 (cdkr.cmd)."""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import account, api, config, launch

REPO_ROOT = Path(__file__).resolve().parents[4]
MARKETPLACE = "ctr-local"
PLUGIN = "codex-cdk-recharge"
SELECTOR = f"{PLUGIN}@{MARKETPLACE}"
_ROW = re.compile(r"^(\S+)@(\S+)\s+(installed\b|not installed)")


def installed_from(listing: str, marketplace: str) -> list[str]:
    """Plugin names that `codex plugin list` reports as installed from one marketplace."""
    rows = (_ROW.match(line.strip()) for line in listing.splitlines())
    return [m.group(1) for m in rows if m and m.group(2) == marketplace and m.group(3) == "installed"]


def _kernel() -> Path | None:
    exe = account.find_kernel()
    if exe is None:
        print("未找到 Codex 桌面 App 的内核 codex.exe（%LOCALAPPDATA%\\OpenAI\\Codex\\bin）。请先安装并打开一次 Codex。")
    return exe


def _run(exe: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)


def _show(proc: subprocess.CompletedProcess) -> None:
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip() and "failed to clean up stale arg0" not in line:
            print("    " + line)


def _confirm(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


def cmd_install(args, paths: config.Paths) -> int:
    exe = _kernel()
    if exe is None:
        return 1
    print(f"使用 Codex App 内核：{exe}")
    cfg_file = Path.home() / ".codex" / "config.toml"
    if cfg_file.exists():
        backup = cfg_file.with_name(f"config.toml.bak-cdkr-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(cfg_file, backup)
        print(f"已备份 Codex 配置：{backup}")
    if MARKETPLACE not in _run(exe, ["plugin", "marketplace", "list"]).stdout:
        print(f"添加本地插件市场：{REPO_ROOT}")
        proc = _run(exe, ["plugin", "marketplace", "add", str(REPO_ROOT)])
        _show(proc)
        if proc.returncode != 0:
            print("添加插件市场失败。")
            return 1
    print(f"安装插件：{SELECTOR}")
    proc = _run(exe, ["plugin", "add", SELECTOR])
    _show(proc)
    if proc.returncode != 0:
        print("安装插件失败。")
        return 1
    print("\n安装完成。接下来：\n"
          "  1. 完全退出并重新打开 Codex App；\n"
          "  2. 插件 → Codex CDK 充值 → 点「信任」（Codex 要求你亲自确认这个 Hook）；\n"
          "  3. 在任意对话的输入框单独发送 /充值，即可打开充值窗口。\n"
          "也可以随时双击「充值.cmd」直接打开窗口。")
    return 0


def cmd_uninstall(args, paths: config.Paths) -> int:
    exe = _kernel()
    if exe is None:
        return 1
    proc = _run(exe, ["plugin", "remove", SELECTOR])
    _show(proc)
    ok = proc.returncode == 0
    listing = _run(exe, ["plugin", "list"])
    others = [n for n in installed_from(listing.stdout, MARKETPLACE) if n != PLUGIN]
    if listing.returncode != 0:  # unsure whether another plugin still uses it: keep it
        print(f"保留插件市场 {MARKETPLACE}（无法确认是否仍有其他插件在用）。")
    elif others:
        print(f"保留插件市场 {MARKETPLACE}（仍有插件在用：{'、'.join(others)}）。")
    else:
        proc = _run(exe, ["plugin", "marketplace", "remove", MARKETPLACE])
        _show(proc)
        ok = ok and proc.returncode == 0
    if paths.root.exists() and (args.yes or _confirm(f"同时删除本机设置和日志（{paths.root}）？输入 Y 确认：")):
        shutil.rmtree(paths.root, ignore_errors=True)
        print("已删除。")
    print("卸载完成，请重启 Codex App。" if ok else "卸载未完全成功，请查看上面的输出。")
    return 0 if ok else 1


def cmd_open(args, paths: config.Paths) -> int:
    launch.open_window()
    return 0


def _describe(base_url: str | None) -> str:
    if not base_url:
        return "未配置（cdkr.cmd config --base-url <地址>）"
    return base_url + ("（本机模拟后台）" if api.is_local(base_url) else "")


def cmd_config(args, paths: config.Paths) -> int:
    cfg = config.load(paths)
    if args.base_url is None and args.timeout is None:
        print(f"充值服务地址：{_describe(cfg.base_url)}")
        print(f"请求超时：{cfg.timeout:g} 秒")
        print(f"配置文件：{paths.config}")
        return 0
    if args.base_url is not None:
        try:
            cfg.base_url = api.check_base_url(args.base_url.strip())
        except api.InsecureUrl as exc:
            print(str(exc))
            return 1
    if args.timeout is not None:
        if not 1 <= args.timeout <= 120:
            print("超时须在 1–120 秒之间。")
            return 1
        cfg.timeout = args.timeout
    config.save(paths, cfg)
    print(f"已保存：{cfg.base_url}，超时 {cfg.timeout:g} 秒。")
    return 0


def cmd_status(args, paths: config.Paths) -> int:
    cfg = config.load(paths)
    print(f"充值服务地址：{_describe(cfg.base_url)}")
    exe = _kernel()
    if exe is None:
        return 1
    installed = PLUGIN in installed_from(_run(exe, ["plugin", "list"]).stdout, MARKETPLACE)
    print(f"插件：{'已安装' if installed else '未安装（双击「安装充值插件.cmd」）'}")
    if installed:
        try:
            result = account.app_server_request([str(exe), "app-server"], "hooks/list", {"cwds": [str(Path.home())]}, 40)
            hooks = [h for entry in result.get("data", []) for h in entry.get("hooks", [])
                     if h.get("pluginId") == SELECTOR or str(h.get("key", "")).startswith(SELECTOR + ":")]
        except (OSError, TimeoutError, RuntimeError, ValueError):
            hooks = None
        if hooks is None:
            print("Hook 状态：读取失败")
        elif not hooks:
            print("Hook 状态：未加载（请重启 Codex App）")
        else:
            status = hooks[0].get("trustStatus")
            hint = "" if status in ("trusted", "managed") else "  → 在 Codex App 的插件页点「信任」后 /充值 才会生效"
            print(f"Hook 状态：{status}{hint}")
    return 0


def main(argv: list[str] | None = None, paths: config.Paths | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cdkr", description="Codex CDK 充值插件")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="用 Codex App 内核安装插件")
    p = sub.add_parser("uninstall", help="卸载插件")
    p.add_argument("--yes", action="store_true", help="同时删除本机设置和日志，不再询问")
    sub.add_parser("open", help="打开充值窗口")
    p = sub.add_parser("config", help="查看或设置充值服务地址")
    p.add_argument("--base-url")
    p.add_argument("--timeout", type=float)
    sub.add_parser("status", help="查看安装与 Hook 信任状态")
    args = parser.parse_args(argv)
    handler = {"install": cmd_install, "uninstall": cmd_uninstall, "open": cmd_open, "config": cmd_config,
               "status": cmd_status}[args.command]
    return handler(args, paths or config.Paths.default())


if __name__ == "__main__":
    sys.exit(main())
