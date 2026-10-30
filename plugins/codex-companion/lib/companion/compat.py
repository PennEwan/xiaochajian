"""Is this Codex one the bundle works with? And if not, what still works (降级提示).

Checked at install, at every control-panel open and on 「重新检测」:
  ok            the App kernel version is one we tested
  untested      newer than the minimum but not tested: install normally, warn
  incompatible  older than the minimum, or the kernel lacks what the plugin needs (the
                `plugin` command, `hooks/list`), or our hooks did not load after install:
                the plugin is not registered; recording and /充值 are unavailable
  unknown       the kernel's version could not be read (a timeout, an App update in progress):
                nothing is changed, the user is asked to check again
  no_codex      the desktop App's kernel was not found
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ctr import kernel

from .product import Product, version_key

OK, UNTESTED, INCOMPATIBLE, NO_CODEX, UNKNOWN = "ok", "untested", "incompatible", "no_codex", "unknown"
CREATE_NO_WINDOW = 0x08000000


@dataclass
class Compat:
    status: str
    message: str
    kernel_path: str | None = None
    kernel_version: str | None = None
    app_version: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def can_record(self) -> bool:
        return self.status in (OK, UNTESTED, UNKNOWN)

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "kernel_path": self.kernel_path,
                "kernel_version": self.kernel_version, "app_version": self.app_version,
                "problems": list(self.problems)}


def degraded_message(product: Product, kernel_version: str | None, why: str) -> str:
    """The degraded-mode notice, worded for after the plugin has been switched off (setup does that)."""
    return (f"当前 Codex（内核 {kernel_version or '未知'}）{why}：「/充值」与任务记录不可用，"
            f"{product.name} 已停用插件，不会记录或上传任何内容。"
            "你仍可从开始菜单打开「Codex 充值」窗口充值（需要能读取 Codex 登录账号）。"
            f"升级 Codex 后，在开始菜单打开「{product.name}」点「重新检测」即可恢复。")


def pending_degrade_message(product: Product, compat_result: "Compat") -> str:
    """For the panel, when the plugin is still registered with an incompatible Codex."""
    return (f"当前 Codex（内核 {compat_result.kernel_version or '未知'}）与 {product.name} 不兼容，"
            "「/充值」与任务记录可能无法正常工作。点「重新检测」将停用插件（不再记录或上传）；"
            "也可以先「暂停任务记录」。充值仍可从开始菜单的「Codex 充值」打开。")


def evaluate(product: Product, kernel_version: str | None, app_version: str | None = None,
             kernel_path: str | None = None, problems: list[str] | None = None) -> Compat:
    problems = list(problems or [])
    base = dict(kernel_path=kernel_path, kernel_version=kernel_version, app_version=app_version, problems=problems)
    if not kernel_path and not kernel_version:
        return Compat(NO_CODEX, "未检测到 Codex 桌面 App。请先安装并打开一次 Codex，"
                                f"然后在开始菜单打开「{product.name}」点「重新检测」。", **base)
    if kernel_version is None:
        return Compat(UNKNOWN, "暂时无法读取 Codex 的版本号（可能正在更新）。没有做任何改动，稍后点「重新检测」重试。",
                      **base)
    if version_key(kernel_version) < version_key(product.min_kernel):
        why = f"低于 {product.name} 支持的最低版本 {product.min_kernel}"
        return Compat(INCOMPATIBLE, degraded_message(product, kernel_version, why), **base)
    if problems:
        why = "缺少插件所需的功能（" + "、".join(problems) + "）"
        return Compat(INCOMPATIBLE, degraded_message(product, kernel_version, why), **base)
    if kernel_version in product.tested_kernels:
        return Compat(OK, f"Codex 内核 {kernel_version}（已验证）", **base)
    return Compat(UNTESTED, f"当前 Codex（内核 {kernel_version}）是未经验证的版本，功能可能受影响；"
                            "如遇问题可在控制面板暂停任务记录。", **base)


def app_version(timeout: float = 20) -> str | None:
    """The desktop App's package version (Get-AppxPackage OpenAI.Codex)."""
    if os.name != "nt":
        return None
    override = os.environ.get("COMPANION_APP_VERSION")
    if override:
        return override
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version | Select-Object -Last 1).Version.ToString()"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out or None


def probe(exe: Path, needs_hooks: bool = True) -> list[str]:
    """What the kernel lacks (empty = has everything the plugin needs). Starts one app-server.

    `needs_hooks=False` for an MCP-only plugin: Codex never runs its hooks, so a kernel that
    cannot list hooks is not a problem.
    """
    problems = []
    try:
        proc = kernel.run(exe, ["plugin", "--help"], timeout=60)
        if proc.returncode != 0 or "marketplace" not in proc.stdout + proc.stderr:
            problems.append("plugin 命令")
    except (OSError, subprocess.SubprocessError):
        problems.append("plugin 命令")
    if not needs_hooks:
        return problems
    from ctr.appserver import AppServer

    server = None
    try:
        server = AppServer(exe, str(Path.home()))
        server.initialize()
        server.request("hooks/list", {"cwds": [str(Path.home())]}, timeout=60)
    except Exception:  # noqa: BLE001 - any failure means the API is not usable
        problems.append("hooks/list")
    finally:
        if server:
            server.close()
    return problems


def detect(product: Product, with_probe: bool = False, with_app: bool = True) -> Compat:
    found = kernel.find_app_kernel()
    app = app_version() if with_app else None
    if not found:
        return evaluate(product, None, app)
    exe, version = found
    problems = probe(exe, needs_hooks=product.declares_hooks()) if with_probe and version else []
    return evaluate(product, version, app, str(exe), problems)
