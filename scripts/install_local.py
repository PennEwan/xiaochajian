"""安装（或诊断安装失败）用的备用入口：不跑 Inno 安装包，直接做安装包做的那几步。

安装包里的每一步都调同一个 `companion setup`，但它用 SW_HIDE 执行、只把结果写进临时文件，
出错时只弹一个笼统的对话框。这个脚本做同样的事，但**把每一步的输出都打出来**，所以哪一步
失败、失败在哪，一眼能看到。

    python scripts\\install_local.py                 # 用最新的 build\\stage-*，装到默认目录
    python scripts\\install_local.py --dry-run       # 只检查，不复制、不注册
    python scripts\\install_local.py --dest D:\\x     # 装到别的目录（例如工作区内，绕开权限问题）
    python scripts\\install_local.py --summary-only   # 已经装过，只打印当前安装状态

装完仍需要在 Codex 里「全部信任」，与安装包一致。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_REL = Path("marketplace") / "plugins" / "codex-companion"
RUNTIME_REL = PLUGIN_REL / "runtime" / "python.exe"
SUMMARY_KEYS = ("status", "message", "terms_version", "collector_config", "collector_task",
                "legacy", "trusted", "config_backup")

# Console code pages differ (cp936 on a Chinese Windows, cp1252 elsewhere, 65001 in VS Code), and a
# UnicodeEncodeError inside an installer-diagnosis script is the last thing a stuck user needs.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

OK, BAD, WARN = "[ OK ]", "[FAIL]", "[ !! ]"


def newest_stage() -> Path | None:
    stages = [p for p in (REPO / "build").glob("stage-*") if (p / PLUGIN_REL / "product.json").is_file()]
    return max(stages, key=lambda p: p.stat().st_mtime) if stages else None


def default_dest() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Programs" / "CodexCompanion"


def probe_writable(path: Path) -> tuple[bool, str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        test = path.parent / f".write-test-{os.getpid()}"
        test.write_text("x", encoding="utf-8")
        test.unlink()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc.strerror or exc}"


def run(cmd: list[str], env: dict) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, cwd=str(Path(tempfile.gettempdir())), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def read_summary(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    return {line.split("=", 1)[0]: line.split("=", 1)[1] for line in text.splitlines()
            if "=" in line}


def explain(summary: dict) -> str:
    task = summary.get("collector_task", "")
    if task == "created":
        return "后台采集任务已注册（任务计划程序 → CodexCompanion\\Collector）。"
    if task == "skipped_dev":
        return "这次是在开发/测试模式下安装（COMPANION_DATA_DIR 被覆盖），跳过了计划任务注册。"
    if task.startswith("schtasks"):
        return ("计划任务注册失败（" + task + "）。常见原因：系统策略禁止创建计划任务、"
                "或该名称的任务已存在且无权限覆盖。可先在「任务计划程序」里删掉 "
                "CodexCompanion\\Collector 再重试。")
    return "计划任务状态：" + (task or "未知")


NEXT_STEPS = """
下一步（与安装包一致）：
  1. 完全退出并重新打开 Codex（托盘里也要退）
  2. 插件 → CDK 充值 → 信任 MCP 服务器 companion_collector（只有一个，没有 12 个 Hook）
  3. 开始菜单「CDK 充值」→「重新检测」→「已在 Codex 中信任插件的 MCP 服务器」应显示完成
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", help="解包目录（默认最新的 build\\stage-*）")
    parser.add_argument("--dest", help="安装目录（默认 %%LOCALAPPDATA%%\\Programs\\CodexCompanion）")
    parser.add_argument("--codex-home", help="覆盖 CODEX_HOME（默认用真实的那份）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    stage = Path(args.stage) if args.stage else newest_stage()
    dest = Path(args.dest) if args.dest else default_dest()
    if not stage or not (stage / PLUGIN_REL / "product.json").is_file():
        print("找不到解包目录：请先运行 python packaging\\build.py --version <v> --profile mock --stage-only")
        return 2
    print(f"解包目录: {stage}\n安装目录: {dest}")

    runtime = dest / RUNTIME_REL
    if not args.summary_only:
        ok, why = probe_writable(dest)
        if not ok:
            print(f"{BAD} 写不了安装目录的上级目录：{why}")
            print("     换一个可写目录试试：python scripts\\install_local.py --dest <别的目录>")
            return 3
        if args.dry_run:
            print(f"{OK} 目录可写；--dry-run 到此为止。")
            return 0
        if dest.exists() and (dest / "unins000.exe").exists():
            print(f"{WARN} 该目录像是一个已安装的产品；继续会覆盖文件（先卸载更干净）。")
        print("复制文件…")
        shutil.copytree(stage, dest, dirs_exist_ok=True)
        if not runtime.is_file():
            print(f"{BAD} 复制后仍找不到运行时：{runtime}")
            return 4
        print(f"{OK} 文件已就位")

    if not runtime.is_file():
        print(f"{BAD} 没有装在这里：{runtime}")
        return 4

    summary_path = Path(tempfile.gettempdir()) / "codex-companion-install.txt"
    summary_path.unlink(missing_ok=True)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if args.codex_home:
        env["CODEX_HOME"] = args.codex_home
    print("向 Codex 注册插件（这一步在安装包里是隐藏执行的）…")
    code, out, err = run([str(runtime), "-m", "companion", "setup", "install",
                          "--install-dir", str(dest), "--summary", str(summary_path)], env)
    if out:
        print("\n".join("  " + line for line in out.splitlines()[:20]))
    if err:
        print("stderr:", err[-800:])
    summary = read_summary(summary_path)
    if not summary:
        print(f"{BAD} 注册步骤没有产出结果（退出码 {code}）。命令：{runtime} -m companion setup install")
        return 5

    print("\n=== 安装结果 ===")
    for key in SUMMARY_KEYS:
        if key in summary:
            print(f"  {key:<18} {summary[key]}")

    status = summary.get("status")
    problem = 0
    if status in ("ok", "untested"):
        print(f"\n{OK} 插件注册完成（status={status}）。")
        if status == "untested":
            print("  注意：" + summary.get("message", ""))
    elif status == "no_codex":
        print(f"\n{BAD} 没有找到 Codex 桌面 App。先安装并打开一次 Codex，然后重跑本脚本（或面板「重新检测」）。")
        problem = 1
    elif status == "incompatible":
        print(f"\n{BAD} Codex 版本不兼容：" + summary.get("message", ""))
        print("  升级 Codex 后重跑；不兼容时充值窗口仍可从开始菜单打开。")
        problem = 1
    else:
        print(f"\n{BAD} 注册未完成（status={status}）：{summary.get('message', '')}")
        problem = 1
    print(explain(summary))
    if problem:
        return 6
    print(NEXT_STEPS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
