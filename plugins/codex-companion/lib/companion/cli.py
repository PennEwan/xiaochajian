"""`python -m companion …` — used by the installer, the Start-menu shortcuts and the test scripts.

    setup install|remove-legacy|prepare-upgrade|uninstall [--result FILE] …
    panel                          the control panel (Start menu「CDK 充值」, /记录)
    status [--json]                everything the panel shows, as text or JSON
    collector status|start|stop    the independent collector's logon task (records without the plugin)
    authz enable|pause|resume|revoke …   the one switch, scripted (enable needs --i-consent)
    update check|download|install|mode … updates
    compat [--json] [--probe]      Codex version check
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import local
from .product import load


def _emit(result: dict, path: str | None) -> None:
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
    print(text)


def summary_lines(result: dict) -> list[str]:
    """key=value lines for the installer's Pascal script, which has no JSON parser."""
    remote = result.get("remote") or {}
    config = result.get("config") or {}
    values = {
        "status": result.get("status", ""),
        "message": result.get("message", ""),
        "legacy": str(len(result.get("legacy") or [])),
        "trusted": str((result.get("hooks") or {}).get("trusted", "")),
        "remote_sent": "" if not remote else ("1" if remote.get("sent") else "0"),
        "remote_deleted": remote.get("deleted") or "",
        "config_verified": "" if not config else ("1" if config.get("verified") else "0"),
        "config_backup": config.get("backup") or result.get("backup") or "",
        "kept": ";".join(result.get("kept") or []),
        "purged": "1" if result.get("purged") else "0",
        "problem": "1" if result.get("left_dirs") or result.get("authz_error") or result.get("plugin_remove") else "0",
        "terms_version": str(result.get("terms_version", "")),
        "collector_task": result.get("collector_task", ""),
        "collector_stop": result.get("collector_stop", ""),
        "collector_ipc": result.get("collector_ipc", ""),
    }
    return [f"{k}={' '.join(str(v).split())}" for k, v in values.items()]


def cmd_setup(args) -> int:
    from . import setup

    ctx = setup.Context.default(load(), Path(args.install_dir) if args.install_dir else None)
    try:
        if args.action == "install":
            result = setup.install(ctx, probe=not args.no_probe)
        elif args.action == "remove-legacy":
            result = setup.remove_legacy(ctx)
        elif args.action == "prepare-upgrade":
            result = setup.prepare_upgrade(ctx)
        else:
            result = setup.uninstall(ctx, delete_remote=args.delete_remote, purge_local=args.purge_local)
    except Exception as exc:  # noqa: BLE001 - the installer must always get a result to show
        result = {"step": args.action, "status": "failed", "message": f"内部错误：{type(exc).__name__}"}
    _emit(result, args.result)
    if args.summary:
        _write_summary(Path(args.summary), result)
    return 0 if result.get("status") in ("ok", "untested") else 2


def _summary_targets(preferred: Path):
    """Where a summary file may go, in order.

    `%TEMP%` is writable on a healthy machine, but security software, a moved profile or a full
    disk can make it not be — and then the installer sees no summary at all and can only say
    "向 Codex 注册插件时出错". Falling back keeps that from being a dead end.
    """
    yield preferred
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "CodexCompanion" / preferred.name
    yield Path.cwd() / preferred.name


def _write_summary(preferred: Path, result: dict) -> None:
    """Write the key=value summary the installer reads. Never raises: an unhandled
    PermissionError inside a windowless step is exactly what produces a blank error dialog."""
    body = "\n".join(summary_lines(result)) + "\n"
    last: OSError | None = None
    for target in _summary_targets(preferred):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8-sig")
            if target != preferred:
                print(f"summary written to {target} (requested path not writable)", file=sys.stderr)
            return
        except OSError as exc:
            last = exc
    print(f"could not write the summary ({type(last).__name__}); result follows:\n{body}",
          file=sys.stderr)


def cmd_panel(args) -> int:
    from . import panel

    return panel.main()


def cmd_status(args) -> int:
    from . import status

    snap = status.collect(load(), with_hooks=not args.offline)
    if args.json:
        print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
    else:
        for label, value in snap.lines():
            print(f"{label}：{value}")
    return 0


def cmd_authz(args) -> int:
    from .authz import Authz, AuthzError

    authz = Authz(load())
    try:
        if args.action == "enable":
            if not args.i_consent:
                print(authz.consent_text())
                print("\n需要明确同意：请在控制面板中开启，或加 --i-consent。")
                return 1
            st = authz.enable(accepted=True)
        elif args.action == "resume":
            st = authz.enable(accepted=False)
        elif args.action == "pause":
            st = authz.pause()
        else:
            print(json.dumps(authz.revoke(args.delete_remote, args.delete_local), ensure_ascii=False))
            st = authz.status()
    except AuthzError as exc:
        print(str(exc))
        return 1
    print(json.dumps(st.__dict__, ensure_ascii=False, default=str))
    return 0


def cmd_update(args) -> int:
    from . import update

    product, cpaths = load(), local.Paths.default()
    if args.action == "mode":
        if args.value:
            update.set_mode(cpaths, args.value)
        print(update.mode(cpaths))
        return 0
    if args.action == "check":
        if args.background:
            from ctr import kernel

            def kernel_version():  # looked up only when an automatic install has to check it
                found = kernel.find_app_kernel()
                return found[1] if found else None

            print(update.background_check(product, cpaths, kernel_version))
            return 0
        doc = update.check(product, cpaths, force=True)
        print(json.dumps(doc, ensure_ascii=False))
        return 0 if not doc.get("error") else 1
    rel = update.available(product, cpaths)
    if rel is None:
        print("没有可用的新版本。")
        return 1
    try:
        path = update.download(product, cpaths, rel)
    except update.UpdateError as exc:
        print(f"下载失败：{exc.kind}")
        return 1
    print(path)
    if args.action == "install":
        update.install(path, silent=args.silent)
    return 0


def cmd_compat(args) -> int:
    from . import compat

    c = compat.detect(load(), with_probe=args.probe)
    print(json.dumps(c.to_dict(), ensure_ascii=False, indent=2) if args.json else f"{c.status}：{c.message}")
    return 0 if c.can_record else 2


# ---- the independent collector -------------------------------------------------------------------
# One line for humans (#8 of the separation design): `记录中 · 已采集 N 条 · 待上传 M 条`, or
# `已停止 · 原因`. The numbers come from the collector itself — its live reply over the local pipe
# first, then the snapshot in collector-status.json; a count it does not report prints as "?".
COLLECTOR_STOPPED_TEXT = {"paused": "已暂停（不记录、不上传）", "stopped": "后台组件已停止",
                          "stopping": "正在停止", "off": "已停止"}
COLLECTOR_REASON_TEXT = {"contract_terminated": "已按你的要求终止记录",
                         "service_terminated": "服务端已终止记录",
                         "server_terminated": "服务端已终止记录",
                         "no_service_state": "尚未取得服务状态"}
COLLECTOR_TERMINATED = ("contract_terminated", "service_terminated", "server_terminated")
COLLECTED_KEYS = ("collected", "collected_events", "events", "events_total", "total_events", "records")
PENDING_KEYS = ("pending", "pending_events", "queued", "queue_depth", "unsent", "spool_pending")


def _collector_snapshot(path: Path) -> dict:
    """`collector-status.json`: what the collector last wrote about itself."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _collector_live(cpaths) -> dict:
    """The running collector's own answer over the pipe; {} when it is not answering."""
    from . import setup

    ipc = setup.collector_module("ipc")
    if ipc is None:
        return {}
    try:
        reply = ipc.call({"op": "status"}, token=ipc.read_token(cpaths.token),
                         root=Path(cpaths.root), timeout=2.0)
    except Exception:  # noqa: BLE001 - a silent collector is one of the answers
        return {}
    return reply if isinstance(reply, dict) else {}


def _collector_count(doc: dict, keys: tuple[str, ...]) -> str:
    """The first count stored under one of `keys`; "?" when the collector does not report it."""
    for key in keys:
        value = doc.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return str(int(value))
        if isinstance(value, str) and value.strip().isdigit():
            return value.strip()
    return "?"


def collector_status_line() -> str:
    """`记录中 · 已采集 N 条 · 待上传 M 条`, or `已停止 · 原因`."""
    from . import setup

    cpaths = setup.collector_paths()
    if cpaths is None:
        return f"已停止 · 采集组件缺失（{setup.COLLECTOR_MISSING}）"
    root = Path(cpaths.root)
    facts = {**_collector_snapshot(cpaths.status), **_collector_live(cpaths)}
    allowed, reason = True, ""
    contract = setup.collector_module("contract")
    if contract is not None and callable(getattr(contract, "may_record", None)):
        try:
            allowed, reason = contract.may_record(root)
        except Exception:  # noqa: BLE001 - an unreadable contract is not a verdict
            allowed, reason = True, ""
    if reason in COLLECTOR_TERMINATED:  # a decision the user made beats every other signal
        return f"已停止 · {COLLECTOR_REASON_TEXT.get(reason, reason)}"
    state = str(facts.get("state") or "").strip().lower()
    if state in COLLECTOR_STOPPED_TEXT:
        return f"已停止 · {COLLECTOR_STOPPED_TEXT[state]}"
    tasks = setup.collector_module("tasks")
    if tasks is not None and callable(getattr(tasks, "exists", None)):
        try:
            registered = bool(tasks.exists(root))
        except Exception:  # noqa: BLE001 - unknown is not a verdict either
            registered = True
        if not registered:
            return "已停止 · 计划任务未注册"
    if not allowed:
        return f"已停止 · {COLLECTOR_REASON_TEXT.get(reason, reason or '已停止')}"
    if not facts:
        return "已停止 · 后台组件未运行"
    return (f"记录中 · 已采集 {_collector_count(facts, COLLECTED_KEYS)} 条"
            f" · 待上传 {_collector_count(facts, PENDING_KEYS)} 条")


def cmd_collector(args) -> int:
    from . import setup

    if args.action == "status":
        print(collector_status_line())
        return 0
    cpaths = setup.collector_paths()
    if cpaths is None:
        print(f"采集组件不可用 · {setup.COLLECTOR_MISSING}")
        return 2
    root = Path(cpaths.root)
    if args.action == "start":  # register (or refresh) the per-user logon task
        ok, detail = setup.collector_call("tasks", "create", root)
        line = f"{'已注册计划任务' if ok else '计划任务注册失败'} · {detail}"
        terminated = setup.collector_call("contract", "is_terminated", root)[0]
        if terminated:
            line += " · 注意：记录已被终止，不会自行恢复"
        print(line)
        return 0 if ok else 2
    ok, detail = setup.collector_call("contract", "terminate", root)  # the durable stop
    ipc = setup.collector_ask_stop(cpaths)  # the immediate one
    ok_task, task = setup.collector_call("tasks", "delete", root)  # and no restart at logon
    print(f"{'已停止' if ok else f'终止未确认 · {detail}'} · 后台进程：{ipc} · 计划任务：{task}")
    return 0 if ok_task and ok else 2


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="companion", description="CDK 充值")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="安装程序调用的步骤")
    p.add_argument("action", choices=["install", "remove-legacy", "prepare-upgrade", "uninstall"])
    p.add_argument("--result", help="把结果 JSON 另存到该文件")
    p.add_argument("--summary", help="安装程序读取的 key=value 摘要（UTF-8 BOM）")
    p.add_argument("--install-dir")
    p.add_argument("--no-probe", action="store_true", help="跳过 Codex 能力探测")
    p.add_argument("--delete-remote", action="store_true", help="卸载：撤销授权并删除后台已上传数据")
    p.add_argument("--purge-local", action="store_true", help="卸载：删除本机记录、充值绑定与设置")
    p.set_defaults(func=cmd_setup)

    sub.add_parser("panel", help="打开控制面板").set_defaults(func=cmd_panel)

    p = sub.add_parser("status", help="状态")
    p.add_argument("--json", action="store_true")
    p.add_argument("--offline", action="store_true", help="不启动 app-server 检查 Hook")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("authz", help="任务记录与上传的开关")
    p.add_argument("action", choices=["enable", "resume", "pause", "revoke"])
    p.add_argument("--i-consent", action="store_true", help="脚本化测试：已阅读并同意授权说明")
    p.add_argument("--delete-remote", action="store_true")
    p.add_argument("--delete-local", action="store_true")
    p.set_defaults(func=cmd_authz)

    p = sub.add_parser("update", help="更新")
    p.add_argument("action", choices=["check", "download", "install", "mode"])
    p.add_argument("value", nargs="?", choices=["notify", "auto", "off"])
    p.add_argument("--background", action="store_true", help="后台检查（Hook 拉起）：到期才检查，auto 模式自动安装")
    p.add_argument("--silent", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("compat", help="Codex 版本兼容检查")
    p.add_argument("--json", action="store_true")
    p.add_argument("--probe", action="store_true")
    p.set_defaults(func=cmd_compat)

    p = sub.add_parser("collector", help="独立采集组件（后台计划任务）")
    p.add_argument("action", choices=["status", "start", "stop"])
    p.set_defaults(func=cmd_collector)

    args = parser.parse_args(argv)
    return args.func(args)
