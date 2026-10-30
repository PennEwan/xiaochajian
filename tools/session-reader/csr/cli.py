"""Command line: scan / watch / stats / coverage / export / purge.

Terminal output never contains prompt, reply, tool or reasoning text; full (redacted)
content only goes to the local database and to files the user exports explicitly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import TextIO

from . import __version__, scanner, summary
from .storage import Storage

APP_DIR = "CodexSessionReader"
DB_NAME = "csr.sqlite3"

STATUS_LABELS = {"unchanged": "无变化", "read": "读取", "rewritten": "文件被替换，已重读",
                 "conflict": "跳过（另一进程正在读取）", "error": "出错"}


class CliError(Exception):
    pass


def default_db(env: dict) -> Path:
    base = env.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / APP_DIR / DB_NAME


def codex_home(env: dict) -> Path:
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex")


def _inside(path: Path, folder: Path) -> bool:
    p, f = os.path.realpath(path), os.path.realpath(folder)
    try:
        return os.path.commonpath([p, f]) == f
    except ValueError:  # different drives
        return False


def _check_location(path: Path, forbidden: list[tuple[Path, str]], what: str) -> None:
    for folder, label in forbidden:
        if _inside(path, folder):
            raise CliError(f"{what}不能放在{label}内：{folder}（本工具对该目录只读）")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="csr", description="只读解析本地 Codex 会话记录（rollout-*.jsonl）")
    p.add_argument("--version", action="version", version=f"csr {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, sessions: bool = True) -> None:
        sp.add_argument("--db", help=f"SQLite 数据库路径（默认 %%LOCALAPPDATA%%\\{APP_DIR}\\{DB_NAME}）")
        if sessions:
            sp.add_argument("--session", action="append", default=[], metavar="ID",
                            help="只处理这些会话（会话 ID 或其前缀，可重复）")

    for name, text in (("scan", "扫描一次指定目录"), ("watch", "前台定时扫描，Ctrl+C 退出")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("--dir", required=True, help="Codex 会话目录（用户明确指定，只读）")
        sp.add_argument("--recursive", action="store_true", help="包含子目录")
        common(sp)
        if name == "watch":
            sp.add_argument("--interval", type=float, default=2.0, help="扫描间隔秒数（默认 2）")
            sp.add_argument("--iterations", type=int, default=0, help=argparse.SUPPRESS)

    sp = sub.add_parser("stats", help="会话统计（不显示正文）")
    common(sp)
    sp.add_argument("--json", action="store_true", help="以 JSON 输出")

    sp = sub.add_parser("coverage", help="事件字段覆盖率")
    common(sp)
    sp.add_argument("--markdown", action="store_true", help="以 Markdown 表格输出")

    sp = sub.add_parser("export", help="导出标准化事件为 JSONL")
    common(sp)
    sp.add_argument("--out", required=True, help="导出文件路径")

    sp = sub.add_parser("purge", help="删除本工具保存的数据")
    common(sp, sessions=False)
    group = sp.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", metavar="ID", help="删除该会话的记录及包含它的导出文件")
    group.add_argument("--all", action="store_true", help="删除数据库及本工具产生的全部导出文件")
    sp.add_argument("--yes", action="store_true", help="不再询问确认")
    return p


def main(argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None,
         env: dict | None = None, stdin: TextIO | None = None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    env = dict(os.environ) if env is None else env
    args = _parser().parse_args(argv)
    db = Path(args.db) if args.db else default_db(env)
    try:
        if args.command in ("scan", "watch"):
            root = Path(args.dir)
            if not root.is_dir():
                raise CliError(f"目录不存在或不是目录：{root}")
            _check_location(db, [(root, "被扫描目录"), (codex_home(env), " Codex 数据目录")], "数据库")
            storage = Storage(db)
        else:
            if not db.exists():
                raise CliError(f"数据库不存在：{db}。请先运行 csr scan --dir <会话目录>")
            storage = Storage(db)
        try:
            handler = {"scan": _scan, "watch": _watch, "stats": _stats, "coverage": _coverage,
                       "export": _export, "purge": _purge}[args.command]
            return handler(args, storage, out, env=env, stdin=stdin or sys.stdin, db=db)
        finally:
            storage.close()
    except CliError as exc:
        print(f"错误：{exc}", file=err)
        return 2


def _short(sid: str) -> str:
    return sid[:13] + "…" if len(sid) > 14 else sid


def _file_line(r: scanner.FileResult) -> str:
    parts = [f"  {r.session_id}  {STATUS_LABELS.get(r.status, r.status)}"]
    if r.new_events:
        parts.append(f"+{r.new_events} 事件")
    if r.new_issues:
        parts.append(f"新问题 {r.new_issues}")
    if r.pending:
        parts.append(f"残行 {r.pending} 字节待续读")
    if r.error:
        parts.append(r.error)
    return "  ".join(parts)


def _scan(args, storage: Storage, out: TextIO, **_) -> int:
    results = scanner.scan(storage, Path(args.dir), recursive=args.recursive, sessions=args.session)
    print(f"扫描目录 {args.dir}（只读{'，含子目录' if args.recursive else ''}）", file=out)
    for r in results:
        print(_file_line(r), file=out)
    total = sum(r.new_events for r in results)
    print(f"共 {len(results)} 个文件，新增事件 {total}；数据库 {storage.path}", file=out)
    print("查看统计：csr stats", file=out)
    return 1 if any(r.status == "error" for r in results) else 0


def _compact(s: dict) -> str:
    models = ", ".join(dict.fromkeys(m["model"] or "?" for m in s["models"])) or "—"
    missing = s["missing"]
    lost = missing["ordinal_gap_records"] + missing["tool_calls_without_result"] + missing["open_turns"]
    return (f"    {_short(s['session_id'])} Prompt {s['prompts']} · 回复 {s['replies']['total']} · "
            f"工具调用 {s['tool_calls']['model_level']} · 模型 {models} · 解析失败 {s['parse_failures']} · "
            f"缺失 {lost}")


def _watch(args, storage: Storage, out: TextIO, **_) -> int:
    print(f"监视目录 {args.dir}（只读），每 {args.interval:g} 秒扫描一次，Ctrl+C 退出", file=out, flush=True)
    current, n = None, 0
    try:
        while True:
            results = scanner.scan(storage, Path(args.dir), recursive=args.recursive, sessions=args.session)
            stamp = time.strftime("%H:%M:%S")
            for r in results:
                if n and not (r.new_events or r.new_issues or r.status in ("rewritten", "error")):
                    continue
                print(f"[{stamp}]{_file_line(r)}", file=out)
                print(_compact(summary.session_stats(storage, r.session_id)), file=out, flush=True)
            latest = summary.current_session(storage)
            if latest and latest != current:
                current = latest
                print(f"[{stamp}] 当前会话：{current}", file=out, flush=True)
            n += 1
            if args.iterations and n >= args.iterations:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已停止（读取进度已保存，下次从断点继续）", file=out)
        return 0


def _selected(args, storage: Storage) -> list[str]:
    sessions = storage.sessions()
    if args.session:
        sessions = [s for s in sessions if any(s.startswith(w.lower()) for w in args.session)]
    return sessions


def _num(value) -> str:
    return f"{value:,}" if isinstance(value, int) else "—"


def _stats_text(s: dict) -> list[str]:
    t, rp, tc, ms = s["turns"], s["replies"], s["tool_calls"], s["missing"]
    files = "；".join(f"{f['name']} 已读 {_num(f['offset'])}/{_num(f['size'])} 字节"
                     + (f"（残行 {f['pending']} 字节待续读）" if f["pending"] else "") for f in s["files"])
    models = " → ".join(f"{m['model'] or '未知'}[{m['reasoning_effort'] or '?'}]×{m['turns']}" for m in s["models"])
    execs = "，".join(f"{k} {v}" for k, v in tc["execution_types"].items()) or "—"
    tok = s["tokens"]
    tokens = (f"合计 {_num(tok.get('total_tokens'))}（输入 {_num(tok.get('input_tokens'))}，缓存 "
              f"{_num(tok.get('cached_input_tokens'))}，输出 {_num(tok.get('output_tokens'))}，推理 "
              f"{_num(tok.get('reasoning_output_tokens'))}）") if tok else "无（记录中没有 token_usage_record）"
    fields = "，".join(f"{k}×{v}" for k, v in ms["missing_fields"].items())
    other = "，".join(f"{k}×{v}" for k, v in s["other_issues"].items()) or "无"
    skipped = "，".join(f"{k}×{v}" for k, v in s["skipped"].items()) or "无"
    return [
        f"会话 {s['session_id']}",
        f"  来源文件    {files or '—'}",
        f"  客户端      {s['originator'] or '未知'} · 内核 {s['codex_version'] or '未知'} · "
        f"{s['first_ts'] or '?'} → {s['last_ts'] or '?'}",
        f"  用户任务    {s['prompts']} 次（回合 {t['started']}：完成 {t['completed']}，出错 {t['failed']}，"
        f"中断 {t['aborted']}，未结束 {t['open']}）",
        f"  模型        {models or '未知'}（切换 {s['model_switches']} 次）",
        f"  模型回复    {rp['total']} 条（final_answer {rp['final_answer']}，commentary {rp['commentary']}"
        + (f"，其他 {rp['other']}" if rp["other"] else "") + "）",
        f"  工具调用    模型级 {tc['model_level']} 次（有结果 {tc['with_result']}，无结果 {tc['without_result']}）；"
        f"执行条目 {tc['executions']} 个（{execs}；失败 {tc['execution_failed']}）",
        f"  推理摘要    公开 {s['reasoning']['public']} 条，不可得 {s['reasoning']['unavailable']} 条",
        f"  Token       {tokens}",
        f"  回合错误    {s['errors']}",
        f"  解析失败    {s['parse_failures']} 行",
        f"  缺失        ordinal 断档 {ms['ordinal_gap_records']} 条；无结果工具调用 {ms['tool_calls_without_result']}；"
        f"未结束回合 {ms['open_turns']}；含缺失字段的事件 {ms['events_with_missing_fields']}"
        + (f"（{fields}）" if fields else ""),
        f"  其他问题    {other}",
        f"  未存储记录  {skipped}",
    ]


def _stats(args, storage: Storage, out: TextIO, **_) -> int:
    sessions = _selected(args, storage)
    stats = [summary.session_stats(storage, sid) for sid in sessions]
    current = summary.current_session(storage)
    if args.json:
        json.dump({"current_session": current, "sessions": stats}, out, ensure_ascii=False, indent=2)
        out.write("\n")
        return 0
    print(f"当前会话：{current or '—'}　共 {len(stats)} 个会话（正文不在终端显示）", file=out)
    for s in stats:
        print("", file=out)
        print("\n".join(_stats_text(s)), file=out)
    return 0


def _coverage(args, storage: Storage, out: TextIO, **_) -> int:
    rows = summary.coverage(storage, _selected(args, storage))
    if args.markdown:
        print(summary.coverage_markdown(rows), file=out)
        return 0
    for r in rows:
        pct = f"{r['present'] / r['applicable']:.0%}" if r["applicable"] else "—"
        print(f"{_pad(r['field'], 30)} {r['present']:>5}/{r['applicable']:<5} {pct:>5}  {r['note']}", file=out)
    return 0


def _pad(text: str, width: int) -> str:
    """Left-align by display width (CJK characters take two columns)."""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(width - shown, 0)


def _export(args, storage: Storage, out: TextIO, *, env: dict, db: Path, **_) -> int:
    target = Path(args.out)
    forbidden = [(codex_home(env), " Codex 数据目录")]
    forbidden += [(Path(f.path).parent, "会话目录") for f in storage.files()]
    _check_location(target, forbidden, "导出文件")
    if os.path.realpath(target) == os.path.realpath(db):
        raise CliError("导出文件不能覆盖数据库")
    sessions = _selected(args, storage)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    count = 0
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for event in storage.iter_events(sessions):
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            count += 1
    os.replace(tmp, target)
    storage.record_export(target, sessions, count)
    print(f"已导出 {count} 条事件（{len(sessions)} 个会话）到 {target}", file=out)
    print("该文件含脱敏后的完整正文；删除：csr purge --all 或 csr purge --session <ID>", file=out)
    return 0


def _confirm(question: str, args, out: TextIO, stdin: TextIO) -> bool:
    if args.yes:
        return True
    print(f"{question}输入 y 确认：", file=out, end="", flush=True)
    return stdin.readline().strip().lower() in ("y", "yes")


def _purge(args, storage: Storage, out: TextIO, *, stdin: TextIO, db: Path, env: dict, **_) -> int:
    if args.session:
        matches = [s for s in storage.sessions() if s.startswith(args.session.lower())]
        if len(matches) != 1:
            found = "没有匹配的会话" if not matches else f"匹配到 {len(matches)} 个会话，请写更长的 ID"
            raise CliError(f"--session {args.session}：{found}")
        sid = matches[0]
        if not _confirm(f"将删除会话 {sid} 的全部记录及包含它的导出文件。", args, out, stdin):
            print("已取消", file=out)
            return 1
        removed = storage.purge_session(sid)
        print(f"已删除会话 {sid} 的记录；删除导出文件 {len(removed)} 个", file=out)
        return 0
    exports = storage.exports()
    if not _confirm(f"将删除数据库 {db} 及 {len(exports)} 个导出文件（不会触碰 Codex 会话文件）。", args, out, stdin):
        print("已取消", file=out)
        return 1
    removed = 0
    for exp in exports:
        if os.path.exists(exp["path"]):
            os.remove(exp["path"])
            removed += 1
    storage.close()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    if os.path.realpath(db.parent) == os.path.realpath(default_db(env).parent):
        try:
            db.parent.rmdir()  # our own folder, and only when nothing else is left in it
        except OSError:
            pass
    print(f"已删除数据库及 {removed} 个导出文件", file=out)
    return 0
