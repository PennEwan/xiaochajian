"""`ctr sync …`: the commands behind 开启同步 / 暂停同步 / 同步状态 / 撤销同步.cmd."""
from __future__ import annotations

import argparse

from .. import cli as core
from .. import state, timeutil
from ..paths import Paths
from . import config, hostgate, launcher, vault
from . import state as sstate
from .api import Api, ApiError, InsecureUrl, check_base_url
from .config import SyncPaths
from .outbox import Outbox
from .worker import PROGRESS, Worker, read_status, run_ops_now

SYNC_CONSENT_TEXT = r"""
================ Codex Task Recorder · 数据同步授权 ================

开启后，本机记录的 Codex 任务内容会在后台持续上传到：
  {base_url}
只上传开启同步之后产生的内容，包括：
  · 你提交的完整 Prompt；模型公开回复与中间消息
  · 模型名称、配置与切换；工具调用、执行结果与错误
  · 公开推理摘要（模型提供时）；任务开始 / 结束 / 中断 / 错误
  · 会话 / 回合 / 工具 ID、时间戳、Token 用量
  · 工作目录、命令与输出中出现的本机路径和文件内容
  · Codex App 在后台自动运行的任务（标记为 background）
不上传：
  · 密码、API Key、令牌、Cookie、私钥、CDK / 兑换码（自动脱敏，基于规则）
  · 凭证文件（auth.json、.env 等）的内容；加密推理与原始推理；系统指令全文
  · 额度与账号信息；记录器诊断信息（本机环境变量、进程链）
身份：后台只看到一个随机设备 ID，不关联你的 ChatGPT 账号。
与充值无关：充值插件不读取这些数据；不开启同步也能正常充值。
暂停：双击「暂停同步.cmd」。查看：双击「同步状态.cmd」。
撤销：双击「撤销同步.cmd」：停止上传、丢弃尚未上传的内容，并可要求后台删除已上传数据。

提示：脱敏基于规则，无法保证识别所有敏感信息，请勿在会话中输入真实密码、CDK 等机密。
====================================================================
"""

ERROR_TEXT = {
    "not_configured": "未配置同步服务地址",
    "insecure_url": "同步服务地址不安全（必须是 https://）",
    "incomplete_results": "后台回复不完整，稍后重发",
    "device_invalid": "后台不认识本设备，将重新注册",
    "device_revoked": "后台已停用此设备",
}


def _error_text(kind: str) -> str:
    if kind in ERROR_TEXT:
        return ERROR_TEXT[kind]
    if "not_sent" in kind:
        return "无法连接同步服务（请求未发出）"
    if "unknown" in kind:
        return "网络中断，结果未确认（会自动重发，后台去重）"
    if "http_5" in kind:
        return "同步服务暂时不可用"
    if "http_429" in kind:
        return "同步服务限流，稍后重试"
    return kind


def state_text(st: sstate.SyncState | None) -> str:
    if st is None:
        return "未开启（从未同意数据同步）"
    if st.corrupt:
        return "○ 未同步（状态文件损坏，按关闭处理；重新开启即可修复）"
    if sstate.is_on(st):
        return "● 同步中"
    return {sstate.PAUSED: "○ 已暂停", sstate.REVOKED: "○ 已撤销授权",
            sstate.DEVICE_REVOKED: "○ 后台已停用此设备（重新开启同步可注册新设备）"}.get(
        st.stopped_reason, "○ 未同步")


def summary_line(paths: Paths) -> str:
    """One line for `ctr status` (记录状态.cmd)."""
    sp = SyncPaths(paths)
    st = sstate.load(sp)
    text = "数据同步：" + state_text(st)
    if sstate.is_on(st):
        events, _ = Outbox(paths).backlog(st)
        text += f"，待上传 {events} 条（详情：同步状态.cmd）"
    return text


def _now():
    return timeutil.now()


DEVICE_ON_OTHER_BACKEND = ("本设备已在原同步后台（{old}）注册。更换后台前请先双击「撤销同步.cmd」"
                           "（可选择删除原后台的数据），否则原后台的数据将无法再删除。")


def _device_elsewhere(sp: SyncPaths, base_url: str) -> str | None:
    dev = vault.load(sp.device)
    old = dev.get("base_url") if dev else None
    return old if old and old != base_url else None


def cmd_enable(args, paths: Paths) -> int:
    sp = SyncPaths(paths)
    cfg = config.load(sp)
    base_url = args.base_url or cfg.base_url
    if not base_url:
        print("尚未配置同步服务地址。请先运行：ctr sync config --base-url https://…")
        return 1
    try:
        base_url = check_base_url(base_url)
    except InsecureUrl as exc:
        print(str(exc))
        return 1
    old = _device_elsewhere(sp, base_url)
    if old:
        print(DEVICE_ON_OTHER_BACKEND.format(old=old))
        return 1
    st = sstate.load(sp)
    if sstate.is_on(st) and not sstate.needs_new_consent(st, base_url):
        print("● 数据同步已经开启。")
        launcher.kick(paths, force=True)
        return 0

    rec = state.load(paths.state)
    if rec is None or rec.corrupt or rec.consent_version != state.CONSENT_VERSION:
        print("数据同步只上传本机记录的内容，需要先同意本地记录：")
        if not core._consent(paths, args.i_consent):
            print("已取消：未开启记录，也未开启数据同步。")
            return 1
    fresh = sstate.needs_new_consent(st, base_url)
    if fresh or args.i_consent:
        print(SYNC_CONSENT_TEXT.format(base_url=base_url))
        if args.i_consent:
            print("已通过 --i-consent 明确同意。")
        elif not core._confirm("同意并开启数据同步？输入 Y 后回车（其他任意键取消）："):
            print("已取消，未开启数据同步。")
            return 1
    elif not core._confirm(f"将恢复数据同步（后台：{base_url}），之后记录的内容会上传。输入 Y 确认："):
        print("已取消，未开启数据同步。")
        return 1

    cfg.base_url = base_url
    config.save(sp, cfg)
    now = _now()
    if not state.is_recording(state.load(paths.state)):
        state.enable(paths.state, now, install_dir=str(core.REPO_ROOT))
        print("● 已开启本地记录。")
    if fresh:  # earlier history is never uploaded: don't even scan it
        Outbox(paths).start_from_now()
    sstate.enable(sp, now, base_url)
    if not args.skip_install:
        print("更新插件代码（不需要重新信任）：")
        if not core.install_plugin():
            print("插件更新未完成：仍会上传，但旧版插件记录的内容可能缺少同步所需字段。")
    launcher.kick(paths, force=True)
    print(f"\n● 已开启数据同步。后台：{base_url}")
    print("  正常使用 Codex 即可，记录的内容会在后台自动上传；Codex 中会提示「正在记录…并同步到后台」。")
    print("  暂停：双击「暂停同步.cmd」；查看：双击「同步状态.cmd」；撤销：双击「撤销同步.cmd」。")
    if not args.skip_install:
        core._print_hook_trust()
    return 0


def cmd_pause(args, paths: Paths) -> int:
    sp = SyncPaths(paths)
    if not sstate.is_on(sstate.load(sp)):
        print("数据同步没有开启，无需暂停。")
        return 0
    sstate.pause(sp, _now())
    print("○ 已暂停数据同步。之后记录的内容不会上传；尚未上传的内容留在本机，恢复后继续上传。")
    print("  恢复：双击「开启同步.cmd」；撤销授权：双击「撤销同步.cmd」。本地记录不受影响。")
    return 0


def cmd_status(args, paths: Paths) -> int:
    sp = SyncPaths(paths)
    st = sstate.load(sp)
    cfg = config.load(sp)
    dev = vault.load(sp.device)
    status = read_status(sp)
    box = Outbox(paths)
    print("数据同步：" + state_text(st))
    print(f"同步后台：{cfg.base_url or '未配置'}")
    print(f"设备 ID：{dev.get('device_id') if dev else '尚未注册（首次上传时注册）'}")
    for w in (st.windows[-3:] if st and not st.corrupt else []):
        print(f"  授权时段：{w.start} → {w.end or '现在'}")
    events, sessions = box.backlog(st if sstate.is_on(st) else None)
    if sstate.is_on(st):
        print(f"待上传：{events} 条（{sessions} 个会话）")
    totals = box.totals()
    outside = totals["skipped:outside_sync_window"] + totals["skipped:before_sync"]
    print(f"已上传：{totals['uploaded']} 条；授权时段外未上传：{outside} 条；后台拒收：{totals['rejected']} 条")
    if totals["skipped:old_schema"]:
        print(f"  旧版插件记录、无法上传：{totals['skipped:old_schema']} 条 → 请运行 ctr install 更新插件")
    if totals["skipped:bad_line"] or totals["skipped:file_rewound"]:
        print(f"  无法解析而跳过：{totals['skipped:bad_line']} 行；会话文件被替换后从头重读："
              f"{totals['skipped:file_rewound']} 次")
    print(f"最近成功上传：{status.get('last_success_at') or '无'}")
    err = status.get("last_error")
    if err:
        print(f"最近错误：{_error_text(err.get('kind', ''))}（{err.get('at')}）")
    running = launcher.worker_running(sp)
    print("上传进程：" + ("运行中" if running else "未运行"))
    blocked = status.get("host_blocked")
    if blocked:
        print(f"上传已停止：{hostgate.REASON_TEXT.get(blocked, blocked)}")
    if st and st.pending_ops:
        names = {sstate.OP_DELETE_REMOTE: "删除后台数据", sstate.OP_REVOKE_DEVICE: "撤销设备"}
        print("待送达后台：" + "、".join(names.get(op, op) for op in st.pending_ops))
    if dev and cfg.base_url and not args.offline:
        try:
            me = Api(dev.get("base_url") or cfg.base_url, timeout=5).me(dev["device_token"])
            print(f"后台已收到：{me.get('event_count')} 条（{me.get('session_count')} 个会话）")
        except (ApiError, InsecureUrl):
            print("后台已收到：暂时无法查询")
    if not running and launcher.kick(paths, st, force=True):
        print("已启动上传进程。")
    return 0


def cmd_revoke(args, paths: Paths) -> int:
    sp = SyncPaths(paths)
    st = sstate.load(sp)
    dev = vault.load(sp.device)
    if st is None and not dev:
        print("数据同步从未开启，无需撤销。")
        return 0
    print("撤销数据同步授权：立即停止上传；尚未上传的内容不会再上传；本机的同步授权记录被清除。")
    print("本地记录不受影响（停止本地记录请双击「停止记录.cmd」）。")
    if not args.yes and not core._confirm("确认撤销？输入 Y 后回车（其他任意键取消）："):
        print("已取消。")
        return 1
    delete = args.delete_remote
    if dev and not delete and not args.keep_remote:
        delete = core._confirm("是否同时删除后台已上传的全部数据？输入 Y 删除（其他任意键保留）：")
    sstate.revoke(sp, _now(), delete_remote=bool(delete))
    report_remote_ops(paths, bool(delete))
    return 0


def report_remote_ops(paths: Paths, delete: bool) -> None:
    """Send the queued delete / revoke now if possible, and say what actually happened."""
    outcome = run_ops_now(paths)
    what = "撤销与删除" if delete else "撤销"
    if outcome is None:
        print(f"本机已停止同步。上传进程正在处理当前请求，{what}请求会由它随后发送；可双击「同步状态.cmd」查看。")
        return
    result, ops = outcome
    if result != PROGRESS:
        print(f"本机已停止同步，但{what}请求暂未送达后台（网络或服务不可用）。")
        print("上传进程会在联网后自动完成；可双击「同步状态.cmd」查看。")
        launcher.kick(paths, force=True)
        return
    print("✔ 已撤销数据同步授权。")
    deleted = ops.get(sstate.OP_DELETE_REMOTE)
    if delete and deleted == "ok":
        print("  后台已删除本设备上传的全部数据。")
    elif delete and deleted == "no_device":
        print("  本设备还没有上传过数据，后台无需删除。")
    elif delete and deleted:
        print(f"  后台未能删除数据（{deleted}），请联系同步服务的运营方处理。")
    elif delete:
        print("  删除请求的结果未知，请双击「同步状态.cmd」查看。")
    if ops.get(sstate.OP_REVOKE_DEVICE, "ok") not in ("ok", "no_device", "unauthorized"):
        print(f"  后台未确认撤销设备（{ops[sstate.OP_REVOKE_DEVICE]}）；本机已删除设备令牌。")


def cmd_config(args, paths: Paths) -> int:
    sp = SyncPaths(paths)
    cfg = config.load(sp)
    if args.base_url:
        try:
            base_url = check_base_url(args.base_url)
        except InsecureUrl as exc:
            print(str(exc))
            return 1
        old = _device_elsewhere(sp, base_url)
        if old:
            print(DEVICE_ON_OTHER_BACKEND.format(old=old))
            return 1
        cfg.base_url = base_url
        config.save(sp, cfg)
        st = sstate.load(sp)
        if sstate.is_on(st) and sstate.needs_new_consent(st, base_url):
            sstate.pause(sp, _now())
            print("○ 已更换同步后台，数据同步已暂停：新后台需要你重新同意（双击「开启同步.cmd」）。")
    print(f"同步后台：{cfg.base_url or '未配置'}")
    return 0


def cmd_run(args, paths: Paths) -> int:
    result = Worker(paths).run(once=args.once)
    totals = Outbox(paths).totals()
    print(f"上传进程结束：{result}；累计已上传 {totals['uploaded']} 条")
    return 0 if result in ("idle", "disabled", "already_running") else 1


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="sync_command", required=True)
    p = sub.add_parser("enable", help="同意并开启数据同步（开启同步.cmd）")
    p.add_argument("--i-consent", action="store_true", help="脚本化测试：明确同意，不再询问")
    p.add_argument("--base-url", help="同步服务地址")
    p.add_argument("--skip-install", action="store_true", help="开发测试：不刷新插件、不检查 Hook 信任")
    p.set_defaults(func=cmd_enable)
    sub.add_parser("pause", help="暂停数据同步（暂停同步.cmd）").set_defaults(func=cmd_pause)
    p = sub.add_parser("status", help="数据同步状态（同步状态.cmd）")
    p.add_argument("--offline", action="store_true", help="不查询后台")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("revoke", help="撤销数据同步授权（撤销同步.cmd）")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--delete-remote", action="store_true", help="同时删除后台已上传的数据")
    group.add_argument("--keep-remote", action="store_true", help="保留后台已上传的数据")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_revoke)
    p = sub.add_parser("config", help="设置同步服务地址")
    p.add_argument("--base-url")
    p.set_defaults(func=cmd_config)
    p = sub.add_parser("run", help="在前台运行上传进程（调试）")
    p.add_argument("--once", action="store_true", help="传完即退出")
    p.set_defaults(func=cmd_run)
