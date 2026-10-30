"""The control panel 「CDK 充值」 (tkinter). Start menu, or /记录 in Codex.

Same card as the recharge window (cdkr.window: aurora background, rounded Win32 region, blue
pill buttons). It shows where setup stands (Codex found, plugin enabled, hooks trusted,
recharge account bound), the task-recording switch with upload progress, and updates; and it
holds the entry points: turn recording on / pause / resume, revoke, recharge, update, uninstall.

Everything slow (status: PowerShell + an app-server; enable / revoke: files and the network)
runs on a worker thread; Tk is touched on the main thread only (results come back through a
queue, as in the recharge window).
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox

from cdkr import paint
from cdkr.paint import PAD_BOTTOM, PAD_TOP, PAD_X, hexc, tint
from cdkr.window import FAMILY, FAINT, INK, MUTED, Chip, RoundButton, aurora_photo, hwnd_of

from . import update
from .authz import ON, PAUSED, AuthzError
from .compat import pending_degrade_message

TITLE = "CDK 充值"
CARD_W = 400
MAX_H = 760
LABEL_W = 78
TONE = {"ok": paint.STATUS_OK, "warn": (238, 170, 60), "bad": paint.BAD_RED, "idle": paint.STATUS_STALE}
TEXT_TONE = {"info": MUTED, "ok": paint.OK_GREEN, "error": paint.BAD_RED}
_GCL_STYLE, _CS_DROPSHADOW = -26, 0x00020000
MUTEX_NAME = "Local\\CodexCompanion.Panel"
DETECTING = "正在检测 Codex、插件与任务记录状态…"
SECONDARY = dict(fill=(255, 255, 255), hot=(236, 242, 248), ink=INK, font=(FAMILY, 9), height=30, pad=12)


def _work(fn, key, results: queue.Queue) -> None:
    try:
        result = (True, fn())
    except Exception as exc:  # noqa: BLE001 - shown to the user, never leaves the panel busy
        result = (False, exc)
    results.put((key, result))


class Actions:
    """What the buttons do; tests substitute a fake."""

    def __init__(self, product, cpaths=None):
        from . import local
        from .authz import Authz

        self.product = product
        self.cpaths = cpaths or local.Paths.default()
        self.authz = Authz(product, cpaths=self.cpaths)

    def collect(self):
        from . import status

        try:  # opening the panel checks for updates too (at most once a day; never when 'off')
            update.check(self.product, self.cpaths)
        except Exception:  # noqa: BLE001 - shown as "检查失败" from update.json, never fatal
            pass
        return status.collect(self.product, cpaths=self.cpaths)

    def recheck(self):
        """「重新检测」: re-register the plugin when Codex can run it but it is not registered, or
        switch it off when Codex cannot; then read everything again."""
        from . import compat, setup, status

        snap = status.collect(self.product, cpaths=self.cpaths, with_hooks=False)
        broken = snap.compat.status == compat.INCOMPATIBLE and snap.plugin.installed
        missing = snap.compat.status in (compat.OK, compat.UNTESTED) and not snap.plugin.installed
        if broken or missing:
            setup.install(setup.Context.default(self.product))
        return self.collect()

    def consent_text(self) -> str:
        return self.authz.consent_text()

    def enable(self, accepted: bool):
        return self.authz.enable(accepted)

    def pause(self):
        return self.authz.pause()

    def revoke(self, delete_remote: bool, delete_local: bool) -> dict:
        return self.authz.revoke(delete_remote, delete_local)

    def open_recharge(self) -> None:
        from cdkr.launch import open_window
        open_window()

    def check_update(self) -> dict:
        return update.check(self.product, self.cpaths, force=True)

    def install_update(self, rel) -> str:
        path = update.download(self.product, self.cpaths, rel)
        update.install(path, silent=False)
        return path

    def set_update_mode(self, value: str) -> None:
        update.set_mode(self.cpaths, value)

    def uninstall(self) -> bool:
        import os
        from pathlib import Path

        from . import launch, local

        install_dir = local.read_json(self.cpaths.state).get("install_dir")
        exe = Path(install_dir) / "unins000.exe" if install_dir else None
        if not exe or not exe.exists():
            return False
        launch.spawn([str(exe)], cwd=os.path.dirname(str(exe)))
        return True


class Panel:
    def __init__(self, actions, run_async=None):
        self.actions = actions
        self.snapshot = None
        self._threaded = run_async is None
        self._run_async = run_async or self._thread_run
        self._results: queue.Queue = queue.Queue()
        self._callbacks: dict = {}
        self._seq = 0
        self._busy = False
        self._closed = False
        self._height = 0
        self._shaped = 0
        self._shadowed = False
        self._drag = (0, 0)
        self.dialog = None

        self.root = tk.Tk()
        self.root.title(TITLE)
        self.root.overrideredirect(True)
        self.root.configure(bg=hexc(paint.CARD_BG))
        self.canvas = tk.Canvas(self.root, width=CARD_W, height=MAX_H, highlightthickness=0, bd=0,
                                bg=hexc(paint.CARD_BG))
        self.canvas.pack(fill="both", expand=True)
        self.rows: list[tuple[int, int, int]] = []
        self._build()

    # ---- card ---------------------------------------------------------------------------------
    def _build(self) -> None:
        c, right = self.canvas, CARD_W - PAD_X
        self._background = aurora_photo(c, "healthy", CARD_W, MAX_H)
        c.create_image(0, 0, image=self._background, anchor="nw")
        c.bind("<Button-1>", self._drag_start)
        c.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<Escape>", lambda _e: self.close())
        self.root.bind("<Configure>", self._on_configure)
        c.create_text(PAD_X, PAD_TOP + 12, text="C O D E X", font=(FAMILY, 9, "bold"), fill=hexc(INK), anchor="w")
        c.create_text(PAD_X + 74, PAD_TOP + 12, text="充值", font=(FAMILY, 9), fill=hexc(MUTED), anchor="w")
        self.version_chip = Chip(c)
        self.version_chip.set("")
        self.close_button = RoundButton(c, self.close, fill=(255, 255, 255), hot=(236, 242, 248), ink=INK,
                                        font=(FAMILY, 9), height=24, text="✕")
        self.close_button._put((right - 24, PAD_TOP, right, PAD_TOP + 24))
        self.headline = c.create_text(PAD_X, PAD_TOP + 50, text="正在检测…", font=(FAMILY, 13, "bold"),
                                      fill=hexc(INK), anchor="w", width=CARD_W - 2 * PAD_X)
        self.hint = c.create_text(PAD_X, 0, text="", font=(FAMILY, 9), fill=hexc(MUTED), anchor="nw",
                                  width=CARD_W - 2 * PAD_X)
        self.primary = RoundButton(c, self._on_primary, text="开启任务记录")
        self.recharge = RoundButton(c, self._on_recharge, **SECONDARY, text="充值")
        self.revoke_button = RoundButton(c, self._on_revoke, **SECONDARY, text="撤销授权…")
        self.update_button = RoundButton(c, self._on_update, **SECONDARY, text="检查更新")
        self.refresh_button = RoundButton(c, self.recheck, **SECONDARY, text="重新检测")
        self.uninstall_button = RoundButton(c, self._on_uninstall, **SECONDARY, text="卸载…")
        self.message = c.create_text(PAD_X, 0, text="", font=(FAMILY, 9), fill=hexc(MUTED), anchor="nw",
                                     width=CARD_W - 2 * PAD_X)
        self.footer = c.create_text(PAD_X, 0, text="", font=(FAMILY, 8), fill=hexc(FAINT), anchor="nw",
                                    width=CARD_W - 2 * PAD_X)
        self.mode_link = c.create_text(0, 0, text="", font=(FAMILY, 8, "underline"), fill=hexc(paint.NOTE_BLUE),
                                       anchor="ne")
        c.tag_bind(self.mode_link, "<Button-1>", lambda _e: self._on_update_mode())
        self._relayout()

    def _set_rows(self, rows: list[tuple[str, str, str]]) -> None:
        c = self.canvas
        for items in self.rows:
            for item in items:
                c.delete(item)
        self.rows = []
        for tone, label, value in rows:
            dot = c.create_oval(0, 0, 0, 0, fill=hexc(TONE.get(tone, TONE["idle"])), outline="")
            lab = c.create_text(0, 0, text=label, font=(FAMILY, 9), fill=hexc(FAINT), anchor="nw")
            val = c.create_text(0, 0, text=value, font=(FAMILY, 9), fill=hexc(INK), anchor="nw",
                                width=CARD_W - 2 * PAD_X - LABEL_W - 14)
            self.rows.append((dot, lab, val))

    def _relayout(self) -> None:
        c, right = self.canvas, CARD_W - PAD_X
        self.version_chip.place(right - 34, PAD_TOP + 12)
        y = c.bbox(self.headline)[3] + 12
        for dot, lab, val in self.rows:
            c.coords(dot, PAD_X, y + 5, PAD_X + 8, y + 13)
            c.coords(lab, PAD_X + 14, y)
            c.coords(val, PAD_X + 14 + LABEL_W, y)
            y = max(c.bbox(lab)[3], c.bbox(val)[3]) + 7
        y += 4
        c.coords(self.hint, PAD_X, y)
        if c.itemcget(self.hint, "text"):
            y = c.bbox(self.hint)[3] + 12
        box = self.primary.place(right, y)
        rbox = self.recharge.place(box[0] - 10, y + 3)
        y = max(box[3], rbox[3]) + 12
        x = right
        for button in (self.uninstall_button, self.refresh_button, self.update_button, self.revoke_button):
            if button["text"]:
                x = button.place(x, y)[0] - 8
            else:
                button.place(x, -100)
        y += SECONDARY["height"] + 14
        c.coords(self.message, PAD_X, y)
        if c.itemcget(self.message, "text"):
            y = c.bbox(self.message)[3] + 10
        c.coords(self.footer, PAD_X, y)
        c.coords(self.mode_link, right, y)
        height = round(max(c.bbox(self.footer)[3], (c.bbox(self.mode_link) or (0, 0, 0, y))[3]) + PAD_BOTTOM)
        if height != self._height:
            self._height = height
            self.root.geometry(f"{CARD_W}x{height}")
            c.configure(height=height)

    # ---- data ---------------------------------------------------------------------------------
    def refresh(self, quiet: bool = False) -> None:
        """Re-read everything. After an action it runs quietly, so the action's result stays visible."""
        if self._busy:
            return
        self._set_busy(True, None if quiet else DETECTING)
        self._run_async(self.actions.collect, self._show)

    def recheck(self) -> None:
        if self._busy:
            return
        self._set_busy(True, DETECTING)
        self._run_async(self.actions.recheck, self._show)

    def _show(self, ok: bool, snap) -> None:
        self._set_busy(False)
        if self.canvas.itemcget(self.message, "text") == DETECTING:
            self._say("", "info")
        if not ok:
            self._say(f"检测失败：{snap}", "error")
            return
        self.snapshot = snap
        self.version_chip.set(f"v{snap.product.version}")
        steps = snap.checklist()
        todo = [s for s in steps[:3] if not s.done]
        c = self.canvas
        if snap.compat.status == "incompatible" and snap.plugin.installed:
            headline, hint = "Codex 版本不兼容", pending_degrade_message(snap.product, snap.compat)
        elif snap.compat.status == "incompatible":
            headline, hint = "Codex 版本不兼容，已降级运行", snap.compat.message
        elif snap.compat.status == "no_codex":
            headline, hint = "未检测到 Codex", snap.compat.message
        elif todo:
            headline, hint = f"还差 {len(todo)} 步完成设置", "下一步：" + todo[0].hint
        else:
            headline, hint = "一切就绪", ""
        if snap.compat.status in ("untested", "unknown") and not todo:
            hint = snap.compat.message
        c.itemconfigure(self.headline, text=headline)
        c.itemconfigure(self.hint, text=hint)
        tone, record = snap.record_line()
        rows = [
            ("ok" if steps[0].done else "bad", "Codex",
             f"App {snap.compat.app_version or '未知'} · 内核 {snap.compat.kernel_version or '未找到'}"),
            ("ok" if steps[1].done else "bad", "插件",
             ("已启用" if snap.plugin.enabled else "已在 Codex 中禁用") if snap.plugin.installed else "未安装"),
            ("ok" if steps[2].done else "warn", "Hook 信任", self._trust_text(snap)),
            (tone, "任务记录", record),
        ]
        if snap.authz.state != "off" or snap.authz.uploaded:
            rows.append(("idle", "上传", snap.upload_line()))
        from .status import error_text

        err = error_text(snap.authz.last_error)
        if err and snap.authz.state == ON:
            rows.append(("warn", "最近错误", err))
        if snap.authz.pending_ops:
            rows.append(("warn", "待送达后台", "撤销 / 删除请求（联网后自动发送）"))
        rel = snap.update_available
        rows.append(("warn" if rel else "idle", "更新",
                     f"有新版本 {rel.version}" + ("（需重新信任 Hook）" if rel.requires_retrust else "") if rel else
                     ("检查失败，稍后自动重试" if snap.update_info.get("error") else "已是最新")))
        self._set_rows(rows)
        state = snap.authz.state
        self.primary.configure(text={ON: "暂停任务记录", PAUSED: "恢复任务记录"}.get(state, "开启任务记录"),
                               state="normal" if self._primary_allowed() else "disabled")
        self.revoke_button.configure(text="撤销授权…" if state != "off" or snap.authz.uploaded else "",
                                     state="normal")
        self.update_button.configure(text=f"更新到 {rel.version}" if rel else "检查更新")
        mode_text = {"notify": "提示我", "auto": "自动安装", "off": "不检查"}[snap.update_mode]
        c.itemconfigure(self.mode_link, text=f"更新方式：{mode_text}")
        c.itemconfigure(self.footer, text="CDK 只在充值窗口输入；任务记录只在你授权期间进行。")
        self._relayout()

    @staticmethod
    def _trust_text(snap) -> str:
        if snap.hooks is not None:
            total = sum(snap.hooks.values())
            if not total:
                return "未加载（重启 Codex）"
            trusted = snap.hooks.get("trusted", 0) + snap.hooks.get("managed", 0)
            text = f"已信任 {trusted}/{total}"
            if snap.hooks.get("modified"):
                text += f"，{snap.hooks['modified']} 个需重新信任"
            if snap.hooks.get("disabled"):
                text += f"，{snap.hooks['disabled']} 个已关闭"
            return text
        return f"已信任 {snap.plugin.hooks_trusted}/{snap.plugin.hooks_total}"

    # ---- actions ------------------------------------------------------------------------------
    def _on_primary(self) -> None:
        snap = self.snapshot
        if snap is None:
            return
        if snap.authz.state == ON:
            self._act(self.actions.pause, "已暂停：之后不记录、不上传；尚未上传的内容留在本机。")
        elif snap.authz.state == PAUSED:
            self._act(lambda: self.actions.enable(False), "已恢复任务记录与上传。")
        else:
            self._ask_consent()

    def _ask_consent(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("开启任务记录 · 授权说明")
        dlg.transient(self.root)
        text = tk.Text(dlg, width=66, height=26, wrap="word", font=(FAMILY, 9), relief="flat", padx=12, pady=10)
        text.insert("1.0", self.actions.consent_text())
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        bar = tk.Frame(dlg, pady=8)
        bar.pack(fill="x")
        self.dialog = dlg

        def accept():
            dlg.destroy()
            self.dialog = None
            self._act(lambda: self.actions.enable(True), "已开启任务记录：之后在 Codex 中的会话会被记录并上传。")

        def cancel():
            dlg.destroy()
            self.dialog = None

        tk.Button(bar, text="取消", width=10, command=cancel).pack(side="right", padx=10)
        tk.Button(bar, text="同意并开启", width=14, command=accept).pack(side="right")
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        self.dialog_accept, self.dialog_cancel = accept, cancel

    def _on_revoke(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("撤销授权")
        dlg.transient(self.root)
        tk.Label(dlg, justify="left", wraplength=420, padx=14, pady=10, font=(FAMILY, 9),
                 text="撤销后立即停止记录与上传，尚未上传的内容不再上传，本机的授权记录被清除。").pack(anchor="w")
        remote, local_ = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        tk.Checkbutton(dlg, text="同时删除后台已上传的全部数据", variable=remote, font=(FAMILY, 9)).pack(anchor="w", padx=14)
        tk.Checkbutton(dlg, text="同时删除本机记录的会话", variable=local_, font=(FAMILY, 9)).pack(anchor="w", padx=14)
        bar = tk.Frame(dlg, pady=8)
        bar.pack(fill="x")
        self.dialog = dlg

        def confirm():
            want_remote, want_local = remote.get(), local_.get()
            dlg.destroy()
            self.dialog = None
            self._act(lambda: self.actions.revoke(want_remote, want_local), None, self._revoked)

        tk.Button(bar, text="取消", width=10, command=lambda: (dlg.destroy(), setattr(self, "dialog", None))) \
            .pack(side="right", padx=10)
        tk.Button(bar, text="确认撤销", width=12, command=confirm).pack(side="right")
        self.revoke_vars, self.dialog_accept = (remote, local_), confirm

    def _revoked(self, result: dict) -> str:
        if not result.get("sent"):
            return "已在本机撤销授权。撤销请求暂未送达后台（网络或服务不可用），联网后会自动发送。"
        deleted = result.get("deleted")
        if deleted == "ok":
            return "已撤销授权，后台已删除本设备上传的全部数据。"
        if deleted == "no_device":
            return "已撤销授权。本设备还没有上传过数据。"
        if deleted:
            return f"已撤销授权，但后台未能删除数据（{deleted}），请联系服务运营方。"
        return "已撤销授权。"

    def _on_recharge(self) -> None:
        try:
            self.actions.open_recharge()
            self._say("已打开充值窗口。CDK 请只在充值窗口中输入。", "info")
        except Exception:  # noqa: BLE001
            self._say("充值窗口没能打开，请从开始菜单打开「Codex 充值」。", "error")

    def _on_update(self) -> None:
        snap = self.snapshot
        rel = snap.update_available if snap else None
        if rel is None:
            self._act(self.actions.check_update, None, lambda doc: (
                "检查失败（网络或更新服务不可用），稍后会自动重试。" if doc.get("error") else "检查完成。"))
            return
        note = "\n\n这个版本修改了插件的 Hook，安装后需要在 Codex 中重新点「全部信任」。" if rel.requires_retrust else ""
        if not messagebox.askyesno(TITLE, f"下载并安装 {rel.version}？\n{rel.notes}{note}\n\n安装时会关闭本面板与充值窗口。",
                                   parent=self.root):
            return
        self._act(lambda: self.actions.install_update(rel), "安装程序已启动。", lambda _p: self.close() or "")

    def _on_update_mode(self) -> None:
        snap = self.snapshot
        order = ["notify", "auto", "off"]
        current = snap.update_mode if snap else "notify"
        nxt = order[(order.index(current) + 1) % len(order)]
        explain = {"notify": "有新版本时提示我，由我决定是否安装。",
                   "auto": "后台自动下载并安装新版本（需要重新信任 Hook 的版本仍会先问我）。",
                   "off": "不检查更新（不会连接更新服务）。"}[nxt]
        if messagebox.askyesno(TITLE, f"把更新方式改为：{explain}", parent=self.root):
            self._act(lambda: self.actions.set_update_mode(nxt), "已更改更新方式。")

    def _on_uninstall(self) -> None:
        if messagebox.askyesno(TITLE, "打开卸载程序？卸载时会询问是否删除后台数据和本机数据。", parent=self.root):
            if self.actions.uninstall():
                self.close()
            else:
                self._say("没有找到卸载程序：请在 Windows「设置 → 应用」中卸载「CDK 充值」。", "error")

    def _act(self, fn, success: str | None, describe=None) -> None:
        if self._busy:
            return
        self._set_busy(True, "正在处理…")

        def done(ok, value):
            self._set_busy(False)
            if not ok:
                self._say(str(value) if isinstance(value, AuthzError) else f"操作失败：{type(value).__name__}",
                          "error")
            else:
                msg = describe(value) if describe else success
                if msg and not self._closed:
                    self._say(msg, "ok")
            if not self._closed:
                self.refresh(quiet=True)

        self._run_async(fn, done)

    # ---- plumbing -----------------------------------------------------------------------------
    def _say(self, text: str, tone: str) -> None:
        self.canvas.itemconfigure(self.message, text=text, fill=hexc(TEXT_TONE[tone]))
        self._relayout()

    def _set_busy(self, busy: bool, text: str | None = None) -> None:
        self._busy = busy
        if self._closed:
            return
        for b in (self.primary, self.revoke_button, self.update_button, self.refresh_button):
            b.configure(state="disabled" if busy else "normal")
        if busy and text:
            self._say(text, "info")
        if not busy and not self._primary_allowed():
            self.primary.configure(state="disabled")

    def _primary_allowed(self) -> bool:
        """Pausing is always possible; turning recording on needs a Codex that can run the plugin."""
        snap = self.snapshot
        return snap is None or snap.authz.state == ON or snap.compat.can_record

    def _thread_run(self, fn, done) -> None:
        self._seq += 1
        self._callbacks[self._seq] = done
        threading.Thread(target=_work, args=(fn, self._seq, self._results), daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                key, (ok, value) = self._results.get_nowait()
                callback = self._callbacks.pop(key, None)
                if callback:
                    callback(ok, value)
        except queue.Empty:
            pass
        finally:
            if not self._closed:
                self.root.after(100, self._drain)

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        self.root.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _on_configure(self, event) -> None:
        if event.widget is self.root and event.height != self._shaped:
            self._shaped = event.height
            if self.root.tk.call("tk", "windowingsystem") != "win32":
                return
            try:
                import ctypes
                hwnd = hwnd_of(self.root)
                radius = round(paint.CARD_RADIUS * 2)
                ctypes.windll.user32.SetWindowRgn(
                    hwnd, ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, CARD_W + 1, event.height + 1, radius, radius),
                    True)
                if not self._shadowed:
                    self._shadowed = True
                    style = ctypes.windll.user32.GetClassLongW(hwnd, _GCL_STYLE)
                    ctypes.windll.user32.SetClassLongW(hwnd, _GCL_STYLE, style | _CS_DROPSHADOW)
            except (AttributeError, OSError, tk.TclError):
                pass

    def close(self) -> None:
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - CARD_W) // 2
        y = max(20, (self.root.winfo_screenheight() - self._height) // 4)
        self.root.geometry(f"+{x}+{y}")
        self.root.after(50, self._front)
        self.root.after(100, self._drain)
        self.refresh()
        self.root.mainloop()

    def _front(self) -> None:
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(400, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()


def main() -> int:
    import ctypes
    import gc
    import os

    from cdkr import single

    from . import local, log
    from .product import load

    if os.name == "nt":
        handle = single.acquire(MUTEX_NAME)
        if handle is None:
            single.raise_existing(TITLE)
            return 0
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    panel = None
    try:
        panel = Panel(Actions(load()))
        panel.run()
    except Exception as exc:  # noqa: BLE001
        log.write(local.Paths.default().log, "panel", type(exc).__name__.lower())
        return 1
    finally:
        panel = None
        gc.collect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
