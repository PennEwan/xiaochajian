"""Command line behind the double-click scripts (安装/开始记录/停止记录/记录状态/卸载)."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

from . import kernel, state, store, timeutil
from .paths import MARKETPLACE_NAME, PLUGIN_SELECTOR, Paths

REPO_ROOT = Path(__file__).resolve().parents[4]

CONSENT_TEXT = r"""
================ Codex Task Recorder · 记录同意说明 ================

开启后，本工具通过 Codex 官方生命周期 Hooks，在本机记录开启期间的【全部 Codex 会话】：
  · 你提交的 Prompt
  · 模型公开返回的消息
  · 模型名称与可获得的配置（推理强度、审批 / 沙箱策略等）
  · 工具调用、执行结果与错误
  · 模型提供时的公开推理摘要
  · 时间戳、会话 ID 与任务关联关系

不会采集：
  · 密码、API Key、令牌、Cookie、私钥（自动脱敏）
  · 凭证文件（如 auth.json、.env）的内容
  · 加密推理内容与原始推理；系统指令（只记哈希）；额度与账户信息

数据位置：{data_dir}
         只保存在本机，本工具不联网、不上传。
停止记录：双击「停止记录.cmd」立即生效；也可在 Codex 中禁用插件，
         或在 设置 → Hooks 中关闭信任。
删除数据：卸载时可选择删除，或运行 ctr purge。

提示：脱敏基于规则，无法保证识别所有敏感信息，请勿在会话中输入真实密码等机密。
====================================================================
"""


def _confirm(question: str) -> bool:
    try:
        answer = input(question)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes", "是", "同意")


def _consent(paths: Paths, assume_yes: bool) -> bool:
    print(CONSENT_TEXT.format(data_dir=paths.root))
    if assume_yes:
        print("已通过 --i-consent 明确同意。")
        return True
    return _confirm("同意并开启记录？输入 Y 后回车（其他任意键取消）：")


def _kernel_or_explain() -> tuple[Path, str | None] | None:
    found = kernel.find_app_kernel()
    if not found:
        print("未找到 Codex 桌面 App 的内核 codex.exe（%LOCALAPPDATA%\\OpenAI\\Codex\\bin）。"
              "请先安装并启动一次 Codex App。")
    return found


def _backup_codex_config() -> Path | None:
    config = Path.home() / ".codex" / "config.toml"
    if not config.exists():
        return None
    backup = config.with_name(f"config.toml.bak-ctr-{timeutil.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(config, backup)
    return backup


def _show(proc) -> None:
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip() and "failed to clean up stale arg0" not in line:
            print("    " + line)


def install_plugin() -> bool:
    found = _kernel_or_explain()
    if not found:
        return False
    exe, version = found
    print(f"使用 Codex App 内核：{exe}（{version}）")
    backup = _backup_codex_config()
    if backup:
        print(f"已备份 Codex 配置：{backup}")
    listing = kernel.run(exe, ["plugin", "marketplace", "list"])
    if MARKETPLACE_NAME not in listing.stdout:
        print(f"添加本地插件市场：{REPO_ROOT}")
        proc = kernel.run(exe, ["plugin", "marketplace", "add", str(REPO_ROOT)])
        _show(proc)
        if proc.returncode != 0:
            print("添加插件市场失败。")
            return False
    print(f"安装插件：{PLUGIN_SELECTOR}")
    proc = kernel.run(exe, ["plugin", "add", PLUGIN_SELECTOR])
    _show(proc)
    if proc.returncode != 0:
        print("安装插件失败。")
        return False
    return True


_LISTING_ROW = re.compile(r"^(\S+)@(\S+)\s+(installed\b|not installed)")


def installed_from(listing: str, marketplace: str) -> list[str]:
    """Plugin names that `codex plugin list` reports as installed from one marketplace."""
    rows = (_LISTING_ROW.match(line.strip()) for line in listing.splitlines())
    return [m.group(1) for m in rows if m and m.group(2) == marketplace and m.group(3) == "installed"]


def uninstall_plugin() -> bool:
    found = _kernel_or_explain()
    if not found:
        return False
    exe, _ = found
    proc = kernel.run(exe, ["plugin", "remove", PLUGIN_SELECTOR])
    _show(proc)
    ok = proc.returncode == 0
    # The marketplace is shared with codex-cdk-recharge; keep it while another plugin uses it.
    own = PLUGIN_SELECTOR.split("@", 1)[0]
    listing = kernel.run(exe, ["plugin", "list"])
    if listing.returncode != 0:  # unsure whether another plugin still uses it: keep it
        print(f"保留插件市场 {MARKETPLACE_NAME}（无法确认是否仍有其他插件在用）。")
        return ok
    others = [n for n in installed_from(listing.stdout, MARKETPLACE_NAME) if n != own]
    if others:
        print(f"保留插件市场 {MARKETPLACE_NAME}（仍有插件在用：{'、'.join(others)}）。")
        return ok
    proc = kernel.run(exe, ["plugin", "marketplace", "remove", MARKETPLACE_NAME])
    _show(proc)
    return ok and proc.returncode == 0


NEXT_STEPS = """
下一步（只需一次）：
  1. 完全退出并重新打开 Codex App。
  2. 打开 插件 → Codex Task Recorder，点击「全部信任」（或在 设置 → Hooks 中逐个信任）。
     Codex 要求你亲自确认 Hook，本工具不会、也不能代你完成这一步。
  3. 正常使用。每个会话第一条消息后会出现「● Codex Task Recorder 正在本地记录本会话」。
"""


def cmd_setup(args, paths: Paths) -> int:
    if not _consent(paths, args.i_consent):
        print("已取消：未安装、未开启记录。")
        return 1
    if not install_plugin():
        print("安装未完成，记录未开启。")
        return 1
    state.enable(paths.state, timeutil.now(), install_dir=str(REPO_ROOT))
    print("\n✔ 已开启记录。")
    print(NEXT_STEPS)
    return 0


def cmd_enable(args, paths: Paths) -> int:
    st = state.load(paths.state)
    if state.is_recording(st):
        print("● 已经在记录中。")
        return 0
    if st is None or st.consent_version != state.CONSENT_VERSION or args.i_consent:
        if not _consent(paths, args.i_consent):
            print("已取消，未开启记录。")
            return 1
    elif not _confirm(f"将重新开启记录（{_scope_text(args.cwd or st.scope_cwds)}，数据保存在 {paths.root}"
                      f"{_sync_hint(paths)}）。输入 Y 确认："):
        print("已取消，未开启记录。")
        return 1
    st = state.enable(paths.state, timeutil.now(), scope_cwds=args.cwd or None, install_dir=str(REPO_ROOT))
    print(f"● 已开启记录。范围：{_scope_text(st.scope_cwds)}")
    return 0


def _sync_hint(paths: Paths) -> str:
    from .sync import state as sstate
    from .sync.config import SyncPaths

    return "；数据同步开启中，新内容会同步到后台" if sstate.is_on(sstate.load(SyncPaths(paths))) else ""


def _scope_text(scope_cwds: list[str] | None) -> str:
    return "仅 " + "、".join(scope_cwds) if scope_cwds else "全部 Codex 会话"


def cmd_disable(args, paths: Paths) -> int:
    st = state.disable(paths.state, timeutil.now())
    print("○ 已停止记录。之后的会话内容不会再被记录。" if st else "记录从未开启，无需停止。")
    if _sync_hint(paths):
        print("  数据同步仍开启，但不会再有新内容可上传。暂停或撤销同步：双击「暂停同步.cmd」/「撤销同步.cmd」。")
    return 0


def _summary(paths: Paths) -> tuple[int, Counter, str | None]:
    sessions: set[str] = set()
    kinds: Counter = Counter()
    last = None
    for e in store.iter_events(paths):
        sessions.add(e.get("session_id"))
        kinds[e.get("kind")] += 1
        last = max(last or "", e.get("recorded_at") or "")
    return len(sessions), kinds, last


def cmd_status(args, paths: Paths) -> int:
    st = state.load(paths.state)
    if st is None:
        print("状态：未开启（从未同意记录）")
    elif st.corrupt:
        print("状态：○ 未记录（状态文件损坏，按停止处理；重新开启即可修复）")
    else:
        print("状态：● 正在记录" if state.is_recording(st) else "状态：○ 已停止")
        print("范围：" + _scope_text(st.scope_cwds))
        for w in st.windows[-5:]:
            print(f"  记录时段：{w.start} → {w.end or '现在'}")
    count, kinds, last = _summary(paths)
    print(f"数据目录：{paths.root}")
    print(f"已记录会话：{count}，事件：{sum(kinds.values())}，最近事件：{last or '无'}")
    if args.verbose:
        for kind, n in kinds.most_common():
            print(f"  {kind}: {n}")
    from .sync.cli import summary_line

    print(summary_line(paths))
    _print_hook_trust()
    return 0


def _print_hook_trust() -> None:
    """Ask Codex itself (hooks/list) whether the plugin's hooks are loaded and trusted."""
    from .appserver import AppServer

    found = kernel.find_app_kernel()
    if not found:
        print("插件 Hook：未找到 Codex App 内核，无法检查")
        return
    server = AppServer(found[0], str(Path.home()))
    try:
        server.initialize()
        hooks = server.plugin_hooks(str(Path.home()), PLUGIN_SELECTOR)
    except Exception as exc:  # noqa: BLE001 - status must still print
        print(f"插件 Hook：检查失败（{type(exc).__name__}）")
        return
    finally:
        server.close()
    if not hooks:
        print("插件 Hook：Codex 未加载本插件的 Hook（插件未安装或已禁用）")
        return
    trust = Counter(h.get("trustStatus") for h in hooks)
    print(f"插件 Hook：已加载 {len(hooks)} 个；" + "，".join(f"{k} {v}" for k, v in trust.items()))
    if trust.get("untrusted") or trust.get("modified"):
        print("  → 还有 Hook 未信任：请在 Codex 插件页点「全部信任」，或在 设置 → Hooks 中信任。")


def cmd_install(args, paths: Paths) -> int:
    return 0 if install_plugin() else 1


def _uninstall_sync(args, paths: Paths) -> bool:
    """Pause sync; offer to revoke it and delete the uploaded data. True while a remote
    revocation is still waiting for the network (its request lives in the sync directory)."""
    from .sync import launcher, vault
    from .sync import state as sstate
    from .sync.cli import report_remote_ops
    from .sync.config import SyncPaths

    sp = SyncPaths(paths)
    st = sstate.load(sp)
    if sstate.is_on(st):
        sstate.pause(sp, timeutil.now())
        print("已暂停数据同步。")
    if vault.load(sp.device) and (args.revoke_sync or _confirm(
            "是否同时撤销数据同步授权，并删除后台已上传的数据？输入 Y 撤销并删除（其他任意键保留）：")):
        sstate.revoke(sp, timeutil.now(), delete_remote=True)
        report_remote_ops(paths, delete=True)
    st = sstate.load(sp)
    if not sstate.wants_worker(st):  # let the uploader exit before its files and plugin go away
        launcher.wait_until_stopped(sp)
    return bool(st and st.pending_ops)


def cmd_uninstall(args, paths: Paths) -> int:
    state.disable(paths.state, timeutil.now())
    sync_pending = _uninstall_sync(args, paths)
    ok = uninstall_plugin()
    print("插件已卸载，记录已停止。" if ok else "卸载命令未全部成功，记录已停止。")
    if paths.root.exists() and (args.purge or _confirm(f"是否同时删除已记录的数据（{paths.root}）？输入 Y 删除：")):
        if sync_pending:  # keep the queued revocation so it can still reach the backend
            for child in paths.root.iterdir():
                if child.name == "sync":
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            print("已删除本地记录数据（保留 sync 目录，以便联网后送达撤销请求）。")
        else:
            shutil.rmtree(paths.root, ignore_errors=True)
            print("已删除本地记录数据。")
    print("如 Codex App 正在运行，请重启它。")
    return 0 if ok else 1


def cmd_purge(args, paths: Paths) -> int:
    if not paths.sessions.exists():
        print("没有已记录的数据。")
        return 0
    if args.session:
        removed = [p for p in paths.sessions.glob("*/*.jsonl") if p.stem == args.session]
        for p in removed:
            p.unlink()
        print(f"已删除 {len(removed)} 个会话文件。")
        return 0
    if args.yes or _confirm("删除全部已记录的会话数据？输入 Y 确认："):
        for sub in (paths.sessions, paths.cursors, paths.notices):
            shutil.rmtree(sub, ignore_errors=True)
        print("已删除全部已记录的会话数据。")
    return 0


def cmd_flush(args, paths: Paths) -> int:
    from .hook import flush_transcripts

    st = state.load(paths.state)
    if st is None:
        print("记录从未开启。")
        return 0
    added = flush_transcripts(paths, st)
    print(f"已补读 transcript 尾部：新增 {added} 条事件（只收授权窗口内的内容）。")
    return 0


def cmd_report(args, paths: Paths) -> int:
    from . import report

    cmd_flush(args, paths)
    md_path, json_path = report.write(paths, copy_to=Path(args.copy_to) if args.copy_to else None)
    print(f"覆盖率报告：{md_path}")
    print(f"机器可读：{json_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctr", description="Codex Task Recorder")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="显示同意说明，安装插件并开启记录（安装.cmd）")
    p.add_argument("--i-consent", action="store_true", help="脚本化测试：明确同意，不再询问")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("enable", help="开启记录（开始记录.cmd）")
    p.add_argument("--i-consent", action="store_true")
    p.add_argument("--cwd", action="append", help="仅开发测试：只记录该目录下的会话（可重复）")
    p.set_defaults(func=cmd_enable)
    sub.add_parser("disable", help="停止记录（停止记录.cmd）").set_defaults(func=cmd_disable)
    p = sub.add_parser("status", help="查看记录状态（记录状态.cmd）")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_status)
    sub.add_parser("install", help="仅安装插件").set_defaults(func=cmd_install)
    p = sub.add_parser("uninstall", help="卸载插件（卸载.cmd）")
    p.add_argument("--purge", action="store_true", help="同时删除已记录的数据")
    p.add_argument("--revoke-sync", action="store_true", help="同时撤销数据同步并删除后台数据")
    p.set_defaults(func=cmd_uninstall)
    p = sub.add_parser("purge", help="删除已记录的数据")
    p.add_argument("--session")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_purge)
    sub.add_parser("flush", help="补读已记录会话的 transcript 尾部").set_defaults(func=cmd_flush)
    p = sub.add_parser("report", help="生成事件覆盖率报告（会先执行 flush）")
    p.add_argument("--copy-to", help="另存一份 Markdown 到该路径")
    p.set_defaults(func=cmd_report)
    from .sync.cli import register as register_sync

    register_sync(sub.add_parser("sync", help="数据同步：enable / pause / status / revoke / config / run"))
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    return args.func(args, Paths.default())
