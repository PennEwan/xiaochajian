"""Everything the control panel shows, gathered off the UI thread (some of it takes seconds:
the App version comes from PowerShell, hook trust from an app-server's hooks/list)."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ctr.paths import Paths as RecorderPaths
from ctr.sync import hostgate

from . import codexcfg, compat, local, update
from .authz import ON, PAUSED, Authz, Status
from .product import Product

ERROR_TEXT = {
    "not_configured": "未配置上传服务地址",
    "backend_changed": "上传服务地址被改动，已停止上传",
    "insecure_url": "上传服务地址不安全",
    "incomplete_results": "后台回复不完整，稍后重发",
    "device_invalid": "后台不认识本设备，将重新注册",
    "device_revoked": "后台已停用此设备",
}


def error_text(kind: str | None) -> str | None:
    if not kind:
        return None
    if kind in ERROR_TEXT:
        return ERROR_TEXT[kind]
    if "not_sent" in kind:
        return "无法连接上传服务（网络不通，内容留在本机，恢复后自动补传）"
    if "unknown" in kind:
        return "网络中断，结果未确认（会自动重发，后台去重）"
    if "http_5" in kind:
        return "上传服务暂时不可用（会自动重试）"
    if "http_429" in kind:
        return "上传服务限流，稍后重试"
    return kind


@dataclass
class Step:
    key: str
    label: str
    done: bool
    hint: str = ""


@dataclass
class Snapshot:
    product: Product
    compat: compat.Compat
    plugin: codexcfg.PluginState
    hooks: dict | None                    # trustStatus -> count, from Codex itself; None = not checked
    authz: Status
    update_mode: str
    update_info: dict
    update_available: update.Release | None
    errors: list[str] = field(default_factory=list)
    collector: dict = field(default_factory=dict)  # the independent component's last self-report

    # ---- derived ------------------------------------------------------------------------------
    @property
    def hooks_trusted(self) -> bool:
        if self.hooks is not None:
            total = sum(self.hooks.values())
            return total > 0 and self.hooks.get("trusted", 0) + self.hooks.get("managed", 0) == total
        return self.plugin.hooks_total > 0 and self.plugin.hooks_trusted == self.plugin.hooks_total

    def checklist(self) -> list[Step]:
        name = self.product.name
        hookless = self.plugin.hooks_total == 0
        trusted = (self.plugin.installed and self.plugin.enabled) if hookless else self.hooks_trusted
        return [
            Step("codex", "检测到 Codex 桌面 App", self.compat.status in (compat.OK, compat.UNTESTED),
                 self.compat.message if self.compat.status not in (compat.OK,) else ""),
            Step("plugin", "插件已安装并启用", self.plugin.installed and self.plugin.enabled,
                 "" if self.plugin.installed and self.plugin.enabled else
                 (f"在 Codex 的插件页启用「{name}」" if self.plugin.installed else
                  "点「重新检测」重新注册插件")),
            Step("trust", "已在 Codex 中信任插件的 MCP 服务器" if hookless else "已在 Codex 中信任插件的 Hook",
                 trusted,
                 "" if trusted else (
                     "Codex 第一次调用 companion_collector 时会请求确认，点「信任」即可（只有一个服务器）"
                     if hookless else
                     f"完全退出并重新打开 Codex → 插件 → {name} → 点「全部信任」（必须由你亲自确认）")),
            Step("record", "后台组件在记录（无需开关）", self.collector.get("recording") is True
                 if self.collector else self.authz.state == ON,
                 "" if (self.collector.get("recording") if self.collector else self.authz.state == ON)
                 else "后台组件未在运行：到任务计划程序里启动 CodexCompanion\\Collector"),
        ]

    def record_line(self) -> tuple[str, str]:
        """What is happening right now, in one row.

        The background component is the recorder, so its own report wins when it exists — the
        plugin-era `authz` state can only say "not enabled", which would be a false statement
        about a component that is running.
        """
        doc = self.collector
        if doc:
            if (doc.get("contract") or {}).get("terminated_at"):
                return "idle", "○ 已停止（合同已终止，可在面板恢复）"
            if not doc.get("recording"):
                return "idle", "○ 已暂停（不记录、不上传）"
            reader, spool = doc.get("reader") or {}, doc.get("spool") or {}
            bits = ["● 记录并上传中"]
            if reader.get("events"):
                bits.append(f"已采集 {reader['events']} 条")
            if spool.get("records"):
                bits.append(f"待上传 {spool['records']} 条")
            if not doc.get("collector_running"):
                return "warn", "后台组件未在运行（记录已停）：任务计划程序 → CodexCompanion\\Collector"
            return "ok", "，".join(bits)
        a = self.authz
        if a.state == ON:
            # What Codex's config says now first; the uploader's last word may be stale (it exits idle).
            blocked = self.plugin.reason or a.host_blocked
            if blocked:
                return "warn", f"已授权，但没有在记录：{hostgate.REASON_TEXT.get(blocked, blocked)}"
            if not a.uploading:
                return "warn", "● 正在本机记录（当前未上传" + (
                    "：后台已停用此设备，点「恢复」可重新注册）" if a.stopped_reason == "device_revoked" else "）")
            return "ok", "● 记录并上传中"
        if a.state == PAUSED:
            return "idle", "○ 已暂停（不记录、不上传）"
        return "idle", "未开启（不记录任何内容）"

    def upload_line(self) -> str:
        a = self.authz
        parts = []
        if a.uploading or a.pending_events:
            parts.append(f"待上传 {a.pending_events} 条")
        parts.append(f"已上传 {a.uploaded} 条")
        if a.rejected:
            parts.append(f"后台拒收 {a.rejected} 条")
        if a.last_success_at:
            parts.append(f"最近上传 {a.last_success_at[:16].replace('T', ' ')}")
        return "，".join(parts)

    def lines(self) -> list[tuple[str, str]]:
        c = self.compat
        rows = [("版本", self.product.version),
                ("Codex", f"App {c.app_version or '未知'}，内核 {c.kernel_version or '未找到'}（{c.status}）"),
                ("插件", ("已启用" if self.plugin.enabled else "已禁用") if self.plugin.installed else "未安装")]
        if self.plugin.hooks_total == 0 and not self.hooks:
            rows.append(("MCP", "companion_collector（本插件不含 Hook，无需逐条信任）"))
        elif self.hooks is not None:
            rows.append(("Hook", "，".join(f"{k} {v}" for k, v in self.hooks.items()) or "未加载"))
        else:
            rows.append(("Hook", f"已信任 {self.plugin.hooks_trusted}/{self.plugin.hooks_total}（来自配置）"))
        rows.append(("任务记录", self.record_line()[1]))
        rows.append(("上传", self.upload_line()))
        err = error_text(self.authz.last_error)
        if err:
            rows.append(("最近错误", err))
        if self.authz.pending_ops:
            rows.append(("待送达后台", "、".join(self.authz.pending_ops)))
        rel = self.update_available
        rows.append(("更新", f"新版本 {rel.version}" if rel else (
            "检查失败（" + self.update_info["error"] + "）" if self.update_info.get("error") else "已是最新")))
        return rows

    def to_dict(self) -> dict:
        return {
            "version": self.product.version, "compat": self.compat.to_dict(),
            "plugin": self.plugin.__dict__, "hooks": self.hooks,
            "authz": self.authz.__dict__, "update_mode": self.update_mode,
            "update_available": self.update_available.to_dict() if self.update_available else None,
            "update_error": self.update_info.get("error"),
            "checklist": [s.__dict__ for s in self.checklist()], "errors": self.errors,
            "collector": self.collector,
        }


def _hook_counts(product: Product) -> dict | None:
    from ctr import kernel
    from ctr.appserver import AppServer

    found = kernel.find_app_kernel()
    if not found:
        return None
    server = None
    try:
        server = AppServer(found[0], str(Path.home()))
        server.initialize()
        hooks = server.plugin_hooks(str(Path.home()), product.plugin_id)
    except Exception:  # noqa: BLE001
        return None
    finally:
        if server:
            server.close()
    counts = Counter(h.get("trustStatus") if h.get("enabled", True) else "disabled" for h in hooks)
    return dict(counts)


def _collector_report() -> dict:
    """The collector's last self-written status. One small file, read off the UI thread."""
    import json
    import os

    try:
        from collector.paths import CollectorPaths

        path = CollectorPaths.default().status
    except (ImportError, OSError):
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return {}
        path = Path(local) / "CodexCompanion" / "collector-status.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def collect(product: Product, with_hooks: bool = True,
            cpaths: local.Paths | None = None, rpaths: RecorderPaths | None = None) -> Snapshot:
    cpaths = cpaths or local.Paths.default()
    errors = []
    c = compat.detect(product, with_probe=False)
    home = codexcfg.codex_home()
    plugin = codexcfg.plugin_state(home, product.root, product.plugin_id, product.marketplace)
    hooks = _hook_counts(product) if with_hooks and c.kernel_path else None
    authz = Authz(product, rpaths, cpaths)
    st = authz.status()
    if st.uploading and not st.worker_running and plugin.reason is None:
        # Looking at the status resumes uploading (the uploader gives up after an hour offline;
        # otherwise only the next hook would start it again) — as 同步状态.cmd did in P3.
        from ctr.sync import launcher

        try:
            launcher.kick(authz.rpaths, force=True)
        except OSError:
            pass
    return Snapshot(product=product, compat=c, plugin=plugin, hooks=hooks,
                    authz=st, update_mode=update.mode(cpaths), update_info=update.info(cpaths),
                    update_available=update.available(product, cpaths), errors=errors,
                    collector=_collector_report())
