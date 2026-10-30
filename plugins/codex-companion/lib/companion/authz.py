"""One switch for task recording + upload (the user's P4 decision).

The bundle shows one consent text covering both, and drives the recorder's consent
(ctr.state) and the sync consent (ctr.sync.state) together:

    enable   consent -> recording on + sync on (a first consent starts uploading from now)
    pause    recording off + sync paused (queued content stays on this machine)
    resume   both back on, without re-reading the text, while the consent version holds
    revoke   both off, consent forgotten, queued content never uploaded; optionally delete the
             uploaded data (sent now when possible) and the local recordings

The local-only recording mode stays available to developers through `ctr` itself.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from ctr import state as rstate
from ctr import timeutil
from ctr.paths import Paths as RecorderPaths
from ctr.sync import api as sapi
from ctr.sync import config as sconfig
from ctr.sync import hostgate, launcher, vault
from ctr.sync import state as sstate
from ctr.sync.config import SyncPaths
from ctr.sync.outbox import Outbox
from ctr.sync.worker import PROGRESS, read_status, run_ops_now

from . import local, log
from .product import Product

CONSENT_VERSION = "p4-1"

CONSENT_TEXT = """开启后，Codex 助手包通过 Codex 官方生命周期 Hooks 记录你【之后】在 Codex 中的全部会话，并在后台持续上传到：
  {base_url}

记录并上传：
  · 你提交的完整 Prompt；模型公开回复与中间消息
  · 模型名称、配置与切换；工具调用、执行结果与错误
  · 公开推理摘要（模型提供时）；任务开始 / 结束 / 中断 / 错误
  · 会话 / 回合 / 工具 ID、时间戳、Token 用量
  · 工作目录、命令与输出中出现的本机路径和文件内容
  · Codex App 在后台自动运行的任务（标记为 background）

不采集、不上传：
  · 密码、API Key、令牌、Cookie、私钥、CDK / 兑换码（自动脱敏，基于规则）
  · 凭证文件（auth.json、.env 等）的内容；加密推理与原始推理；系统指令全文
  · 额度与账号信息；本机环境变量与进程信息
  · 开启之前的任何内容；暂停期间的任何内容

身份：后台只看到一个随机设备 ID，不关联你的 ChatGPT 账号（充值绑定的账号也不会用于上传）。
本机副本：{data_dir}
暂停 / 恢复：在 Codex 中发送 /记录，或打开开始菜单「Codex 助手包」。
撤销：在控制面板点「撤销授权」：停止记录与上传、丢弃尚未上传的内容，并可要求后台删除已上传数据。
在 Codex 中禁用本插件、关闭它的 Hook 或卸载，也会立即停止记录和上传。

提示：脱敏基于规则，无法保证识别所有敏感信息，请勿在会话中输入真实密码、CDK 等机密。"""

ON, PAUSED, OFF = "on", "paused", "off"


class AuthzError(Exception):
    """A user-facing reason the switch cannot be turned on."""


@dataclass
class Status:
    state: str                         # on / paused / off
    recording: bool = False
    uploading: bool = False
    consented: bool = False
    base_url: str | None = None
    pending_events: int = 0
    uploaded: int = 0
    rejected: int = 0
    last_success_at: str | None = None
    last_error: str | None = None
    worker_running: bool = False
    host_blocked: str | None = None
    pending_ops: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


class Authz:
    def __init__(self, product: Product, rpaths: RecorderPaths | None = None,
                 cpaths: local.Paths | None = None, now=timeutil.now):
        self.product = product
        self.rpaths = rpaths or RecorderPaths.default()
        self.sp = SyncPaths(self.rpaths)
        self.cpaths = cpaths or local.Paths.default()
        self.now = now

    # ---- consent ------------------------------------------------------------------------------
    def consent_text(self) -> str:
        return CONSENT_TEXT.format(base_url=self.product.sync_base_url or "（未配置）", data_dir=self.rpaths.root)

    def _consent(self) -> dict:
        return local.read_json(self.cpaths.state)

    def _product_url(self) -> str | None:
        """The build's upload address, normalised the way the sync consent records it."""
        try:
            return sapi.check_base_url(self.product.sync_base_url) if self.product.sync_base_url else None
        except sapi.InsecureUrl:
            return None

    def consented(self) -> bool:
        doc = self._consent()
        st = sstate.load(self.sp)
        url = self._product_url()
        return (doc.get("consent_version") == CONSENT_VERSION and st is not None and not st.corrupt
                and bool(url) and not sstate.needs_new_consent(st, url))

    def _base_url(self) -> str:
        url = self.product.sync_base_url
        if not url:
            raise AuthzError("这个安装包没有配置数据上传服务地址，无法开启任务记录。")
        try:
            url = sapi.check_base_url(url)
        except sapi.InsecureUrl as exc:
            raise AuthzError(str(exc)) from None
        dev = vault.load(self.sp.device)
        if dev and dev.get("base_url") and dev["base_url"] != url:
            raise AuthzError("本设备已在另一个上传服务注册过。请先「撤销授权」（可同时删除那里的数据），再重新开启。")
        return url

    def _check_host(self) -> None:
        """The plugin is only a UI now, so the only host verdict that still matters is whether
        Codex knows the plugin at all — per-hook trust is not part of this product."""
        reason = hostgate.Gate(self.product.root).check()
        if reason in (hostgate.UNINSTALLED, hostgate.DISABLED):
            raise AuthzError(f"{hostgate.REASON_TEXT[reason]}：请先在 Codex 的插件页启用「{self.product.name}」。")

    def _collector_op(self, op: str) -> None:
        """Forward the same switch to the independent collector when it is running.

        Best effort by design: the collector holds its own state on disk, so a panel action must
        never fail because the pipe is down (it is down until the logon task starts it).
        """
        try:
            from collector import ipc
            from collector.paths import CollectorPaths
        except ImportError:
            return
        try:
            root = CollectorPaths.default().root
            ipc.call({"op": op}, token=ipc.read_token(CollectorPaths(root).token), root=root, timeout=5.0)
        except (ipc.IpcError, OSError, ValueError):
            pass

    # ---- switch -------------------------------------------------------------------------------
    def enable(self, accepted: bool) -> Status:
        """Turn recording + upload on. A first (or changed) consent needs accepted=True."""
        url = self._base_url()
        self._check_host()
        fresh = not self.consented()
        if fresh and not accepted:
            raise AuthzError("需要先阅读并同意授权说明。")
        now = self.now()
        cfg = sconfig.load(self.sp)
        cfg.base_url = url
        sconfig.save(self.sp, cfg)
        rstate.enable(self.rpaths.state, now, install_dir=str(self.product.root))
        if fresh:  # earlier history is never uploaded, and never scanned again
            Outbox(self.rpaths).start_from_now()
            local.update_json(self.cpaths.state, consent_version=CONSENT_VERSION,
                              consent_accepted_at=timeutil.iso(now))
        sstate.enable(self.sp, now, url)
        log.write(self.cpaths.log, "authz", "enable" if fresh else "resume")
        self._collector_op("resume")
        try:
            launcher.kick(self.rpaths, force=True)
        except OSError:
            pass
        return self.status()

    def pause(self) -> Status:
        now = self.now()
        rstate.disable(self.rpaths.state, now)
        if sstate.is_on(sstate.load(self.sp)):
            sstate.pause(self.sp, now)
        self._collector_op("pause")
        launcher.wait_until_stopped(self.sp, 3.0)
        log.write(self.cpaths.log, "authz", "pause")
        return self.status()

    def revoke(self, delete_remote: bool, delete_local: bool) -> dict:
        """Returns what happened remotely: {'sent': bool, 'deleted': outcome or None}."""
        now = self.now()
        rstate.disable(self.rpaths.state, now)
        st = sstate.load(self.sp)
        result = {"sent": True, "deleted": None, "registered": bool(vault.load(self.sp.device))}
        if st is not None or result["registered"]:
            sstate.revoke(self.sp, now, delete_remote=delete_remote)
            outcome = run_ops_now(self.rpaths)
            if outcome is None or outcome[0] != PROGRESS:
                result["sent"] = False
                try:
                    launcher.kick(self.rpaths, force=True)
                except OSError:
                    pass
            else:
                result["deleted"] = outcome[1].get(sstate.OP_DELETE_REMOTE) if delete_remote else None
        local.update_json(self.cpaths.state, consent_version=None, consent_accepted_at=None,
                          revoked_at=timeutil.iso(now))
        self._collector_op("stop")
        if delete_local:
            self.delete_local_recordings()
        log.write(self.cpaths.log, "authz", "revoke", sent=int(result["sent"]))
        return result

    def delete_local_recordings(self) -> None:
        for sub in (self.rpaths.sessions, self.rpaths.cursors, self.rpaths.notices, self.rpaths.reports):
            shutil.rmtree(sub, ignore_errors=True)
        shutil.rmtree(self.sp.cursors, ignore_errors=True)

    # ---- status -------------------------------------------------------------------------------
    def status(self) -> Status:
        rec = rstate.load(self.rpaths.state)
        st = sstate.load(self.sp)
        recording, uploading = rstate.is_recording(rec), sstate.is_on(st)
        box = Outbox(self.rpaths)
        pending = box.backlog(st)[0] if uploading else 0
        totals = box.totals()
        worker = read_status(self.sp)
        err = worker.get("last_error")
        return Status(
            state=ON if recording else (PAUSED if self.consented() else OFF),
            recording=recording, uploading=uploading, consented=self.consented(),
            base_url=st.consent_base_url if st and not st.corrupt else None,
            pending_events=pending, uploaded=totals["uploaded"], rejected=totals["rejected"],
            last_success_at=worker.get("last_success_at"),
            last_error=err.get("kind") if isinstance(err, dict) else None,
            worker_running=launcher.worker_running(self.sp),
            host_blocked=worker.get("host_blocked"),
            pending_ops=list(st.pending_ops) if st and not st.corrupt else [],
            stopped_reason=st.stopped_reason if st and not st.corrupt else None,
        )
