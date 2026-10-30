"""`python -m collector …` — command line for the background component."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config as ccfg, contract, ipc, tasks
from .paths import CollectorPaths
from .spool import Spool

# `collector.run` pulls in the sync client and the session reader; importing it here would make the
# MCP server (spawned per Codex session) depend on those. Load it only for the commands that run.


def _root(args) -> Path:
    return Path(args.data_dir) if getattr(args, "data_dir", None) else CollectorPaths.default().root


def _call(root: Path, op: str) -> dict:
    try:
        return ipc.call({"op": op}, token=ipc.read_token(CollectorPaths(root).token), root=root)
    except ipc.IpcError as exc:
        return {"ok": False, "error": exc.kind}


def cmd_status(args) -> int:
    root = _root(args)
    running = ipc.ping(token=ipc.read_token(CollectorPaths(root).token), root=root)
    if running:
        reply = _call(root, "status")
    else:
        try:
            reply = json.loads(CollectorPaths(root).status.read_text(encoding="utf-8"))
            reply["collector_running"] = False
        except (OSError, ValueError):
            reply = {"ok": True, "collector_running": False, "recording": False,
                     "gate": "not_running"}
    if args.json:
        print(json.dumps(reply, ensure_ascii=False, indent=2))
        return 0
    state = "记录中" if reply.get("recording") else "已停止"
    reader = reply.get("reader") or {}
    spool = reply.get("spool") or {}
    bits = [f"{state}",
            f"已采集 {reader.get('events', 0)} 条",
            f"待上传 {spool.get('records', 0)} 条"]
    if not reply.get("collector_running"):
        bits.append("后台组件未运行")
    elif reply.get("gate") not in (None, "active", "grace"):
        bits.append(f"原因：{reply.get('gate')}")
    if reply.get("upload", {}).get("last_error"):
        bits.append(f"上传错误：{reply['upload']['last_error']}")
    print(" · ".join(bits))
    return 0


def cmd_control(args) -> int:
    root = _root(args)
    if args.op == "stop":
        contract.terminate(root, "user")
    if args.op == "resume":
        contract.resume(root)  # clears the user's own stop; a backend stop stays
    if args.op in ("pause", "resume"):
        ccfg.set_paused(root, args.op == "pause")
    reply = _call(root, args.op)
    if not reply.get("ok"):
        print(f"{args.op}: 后台组件未运行（{reply.get('error')}），已写入本地状态")
        return 0
    print(json.dumps({k: reply.get(k) for k in ("ok", "recording", "paused", "gate")},
                     ensure_ascii=False))
    return 0


def cmd_task(args) -> int:
    root = _root(args)
    if args.op == "install":
        ok, detail = tasks.create(root)
    elif args.op == "uninstall":
        ok, detail = tasks.delete(root)
    else:
        ok, detail = tasks.exists(root), "exists" if tasks.exists(root) else "absent"
    print(json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_contract(args) -> int:
    root = _root(args)
    doc = contract.ensure_contract(root) if args.init else contract.load_contract(root)
    if not doc:
        print("尚无合同记录（未安装，或安装时未写入）")
        return 0
    print(json.dumps({k: doc.get(k) for k in
                      ("terms_version", "terms_hash", "effective_at", "first_run_at", "terminated_at")},
                     ensure_ascii=False, indent=2))
    if args.text:
        print("\n" + str(doc.get("terms_text") or contract.TERMS_TEXT))
    return 0


def cmd_run(args) -> int:
    from .run import Collector  # lazy: `mcp` must not pay for the sync client and the reader

    root = _root(args)
    Collector(root).run(once=args.once)
    return 0


def cmd_scan(args) -> int:
    from .run import Collector

    root = _root(args)
    col = Collector(root)
    try:
        col.control.stop("once")
        col.refresh_service_state(0.0)
        allowed, reason = contract.may_record(root)
        print(f"gate: {reason}")
        if not allowed:
            return 0
        n = col.collect(0.0)
        print(json.dumps({"records": n, "scan": col.scan_result,
                          "spool": col.spool.depth()}, ensure_ascii=False))
    finally:
        col.reader.close()
    return 0


def cmd_purge(args) -> int:
    root = _root(args)
    paths = CollectorPaths(root)
    segments = Spool(paths.spool).purge()
    extra = 0
    for target in (paths.db, paths.db.with_name(paths.db.name + "-wal"), paths.db.with_name(paths.db.name + "-shm")):
        try:
            target.unlink()
            extra += 1
        except OSError:
            pass
    print(json.dumps({"segments": segments, "files": extra}, ensure_ascii=False))
    return 0


def cmd_mcp(args) -> int:
    from . import mcp
    argv = []
    if getattr(args, "data_dir", None):
        argv += ["--data-dir", str(args.data_dir)]
    return mcp.main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collector", description=f"codex-companion collector {__version__}")
    parser.add_argument("--data-dir", help="数据目录（默认 %%LOCALAPPDATA%%\\CodexCompanion）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="前台运行后台组件（计划任务用这个）")
    p.add_argument("--once", action="store_true", help="只跑一轮")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="一行状态")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("scan", help="立即扫描一次并写入队列，不上传")
    p.set_defaults(func=cmd_scan)

    for op, help_text in (("pause", "暂停记录与上传"), ("resume", "恢复"),
                          ("stop", "停止记录（写终止时间，进程退出）")):
        p = sub.add_parser(op, help=help_text)
        p.set_defaults(func=cmd_control, op=op)

    p = sub.add_parser("task", help="安装 / 卸载登录计划任务")
    p.add_argument("op", choices=["install", "uninstall", "status"])
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("contract", help="查看合同记录")
    p.add_argument("--init", action="store_true", help="没有就创建")
    p.add_argument("--text", action="store_true", help="打印条款全文")
    p.set_defaults(func=cmd_contract)

    p = sub.add_parser("purge", help="删除本机队列与解析库")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("mcp", help="MCP server（stdio）")
    p.set_defaults(func=cmd_mcp)

    # `--data-dir` is accepted before *and* after the subcommand; SUPPRESS keeps a value given
    # at the top level from being overwritten by the subparser's default.
    for child in sub.choices.values():
        child.add_argument("--data-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
