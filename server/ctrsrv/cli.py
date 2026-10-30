"""`python -m ctrsrv …` — serve the API or run one operational command."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, terms
from .app import Config, serve
from .store import Store


def _store(args) -> Store:
    return Store(Path(args.db) if args.db else Config().db)


def cmd_serve(args) -> int:
    # Only override what was actually passed: `Config()` reads CTRSRV_HOST / CTRSRV_PORT /
    # CTRSRV_DB / CTRSRV_ADMIN_TOKEN, and a deployment that sets them via the environment must not
    # be silently reset to the argparse defaults.
    config = Config()
    if args.db:
        config.db = Path(args.db)
    if args.host is not None:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    if args.admin_token is not None:
        config.admin_token = args.admin_token
    serve(config)
    return 0


def cmd_initdb(args) -> int:
    store = _store(args)
    store.connect()
    print(json.dumps({"ok": True, "db": str(store.path), "terms_version": terms.TERMS_VERSION,
                      "terms_hash": terms.TERMS_HASH}, ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_add_cdk(args) -> int:
    store = _store(args)
    for code in args.codes:
        store.add_cdk(code, args.product, args.expires)
    print(json.dumps({"ok": True, "added": len(args.codes)}, ensure_ascii=False))
    return 0


def cmd_stats(args) -> int:
    store = _store(args)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_devices(args) -> int:
    store = _store(args)
    for device in store.iter_devices(args.limit, args.offset):
        if args.json:
            print(json.dumps(device, ensure_ascii=False))
            continue
        print(f"{device['device_id']}  {device['state']:<10} events={device['event_count']:<7} "
              f"sessions={device['session_count']:<5} created={device['created_at']} "
              f"client={device.get('client') or '-'}")
    return 0


def cmd_events(args) -> int:
    store = _store(args)
    for row in store.iter_events(args.device, args.limit, args.offset):
        if args.raw:
            print(row["body"])
        else:
            print(f"{row['received_at']}  {row['device_id']}  {row['session_id']}  seq={row['seq']}  "
                  f"{row['tier'] or '-':<2} {row['kind'] or '-':<20} bytes={len(row['body'])}")
    return 0


def cmd_device_action(args) -> int:
    store = _store(args)
    if args.op == "terminate":
        print(json.dumps(store.terminate(args.device, "server"), ensure_ascii=False))
    elif args.op == "resume":
        store.set_state(args.device, "active")
        print(json.dumps({"ok": True, "state": "active"}, ensure_ascii=False))
    elif args.op == "purge":
        print(json.dumps({"ok": True, "deleted_events": store.purge_device(args.device)},
                         ensure_ascii=False))
    return 0


def cmd_prune(args) -> int:
    store = _store(args)
    days = max(1, args.days)  # `--days 0` would delete everything; that is not a retention policy
    print(json.dumps({"ok": True, "days": days, "deleted_events": store.prune_events(days)},
                     ensure_ascii=False))
    return 0


def _text_of(data) -> str:
    if isinstance(data, dict):
        for key in ("text", "message", "output", "summary_text", "command", "last_agent_message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if data:
            return json.dumps(data, ensure_ascii=False)
    return "" if data is None else str(data)


def cmd_view(args) -> int:
    """Read a session like a transcript: `view --latest`, or `--device X --session Y`."""
    store = _store(args)
    device_id, session_id = args.device, args.session
    if args.latest or not (device_id and session_id):
        found = store.latest_session()
        if found is None:
            print("后台还没有任何事件。")
            return 1
        device_id, session_id = found if args.latest or not device_id else (device_id, None)
    if session_id is None:
        sessions = store.sessions_for(device_id)
        print(f"设备 {device_id} 的会话：")
        for s in sessions:
            tiers = " ".join(f"{k}:{v}" for k, v in (s.get("tiers") or {}).items())
            print(f"  {s['session_id']}  {s['events']} 条  [{tiers}]  {s.get('last_ts') or ''}")
        print("\n用 --session <id> 看具体会话，或 --latest 看最新一条。")
        return 0
    events = store.events_for_session(device_id, session_id)
    if not events:
        print(f"没有会话 {session_id}。")
        return 1
    if args.raw:
        for row in events:
            print(row["body"])
        return 0
    print(f"会话 {session_id}（设备 {device_id}，{len(events)} 条记录）\n")
    seen: set[tuple] = set()
    for row in events:
        try:
            doc = json.loads(row["body"])
        except ValueError:
            continue
        text = _text_of(doc.get("data"))
        if not text:
            continue
        # One logical event lands once per tier; show the richest copy only.
        key = (doc.get("kind"), doc.get("seq"), text[:120])
        if key in seen:
            continue
        seen.add(key)
        stamp = (row.get("received_at") or "")[11:19]
        print(f"[{stamp}] {row.get('tier') or '-':<2} {row.get('kind') or '-':<20} {text}")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctrsrv", description=f"Codex 采集后台 {__version__}")
    parser.add_argument("--db", default=None,
                        help="SQLite 路径（默认 CTRSRV_DB 或 data/ctrsrv.sqlite3）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    p.add_argument("--host", default=None, help="默认 CTRSRV_HOST 或 127.0.0.1")
    p.add_argument("--port", type=int, default=None, help="默认 CTRSRV_PORT 或 8080")
    p.add_argument("--admin-token", default=None,
                   help="默认 CTRSRV_ADMIN_TOKEN；都没有则 /admin 全部 403 admin_disabled")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("initdb", help="建库并打印条款版本与哈希")
    p.set_defaults(func=cmd_initdb)

    p = sub.add_parser("add-cdk", help="导入 CDK")
    p.add_argument("codes", nargs="+")
    p.add_argument("--product", default=None)
    p.add_argument("--expires", default=None, help="过期时间（ISO，UTC）")
    p.set_defaults(func=cmd_add_cdk)

    p = sub.add_parser("stats", help="总量统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("devices", help="设备列表")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("events", help="事件列表（默认不打印正文）")
    p.add_argument("--device")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--raw", action="store_true", help="打印原始 JSON 正文")
    p.set_defaults(func=cmd_events)

    for op in ("terminate", "resume", "purge"):
        p = sub.add_parser(op, help=f"{op} 一台设备")
        p.add_argument("device")
        p.set_defaults(func=cmd_device_action, op=op)

    p = sub.add_parser("prune", help="按保留期删除事件")
    p.add_argument("--days", type=int, default=90)
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("view", help="按会话读成文字（默认最新一条）")
    p.add_argument("--device")
    p.add_argument("--session")
    p.add_argument("--latest", action="store_true")
    p.add_argument("--raw", action="store_true", help="打印原始 JSON 行")
    p.set_defaults(func=cmd_view)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
