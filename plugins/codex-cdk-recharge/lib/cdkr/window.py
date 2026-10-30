"""The recharge window (tkinter, standard library). Run as `pythonw -m cdkr.window`.

A borderless rounded card in the style of the Codex 小助手 quota card: aurora background,
ink text, blue action button (see paint.py for the palette). Tk has no alpha or gradients,
so the background is a PhotoImage painted row by row and the corners are clipped with a
Win32 region.

Only display and input live here; every decision is in flow.Recharge, executed on a
worker thread so the window never freezes. The user-entered account and CDK are sent only
to the recharge service, and the CDK is cleared after a successful redemption.
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox

from . import api, flow, log, paint
from .paint import CARD_W, PAD_BOTTOM, PAD_TOP, PAD_X, hexc, round_rect_points, tint

TITLE = "Codex 充值"
GUIDE_TEXT = "请输入要充值的 ChatGPT 账号；账号和 CDK 只会发送给充值服务。"
# Experiment: the guide link opens the ChatGPT session endpoint in the default browser.
# It is a plain navigation — the window never reads what the page shows.
RECHARGE_URL = "https://chatgpt.com/api/auth/session?utm_source=chatgpt.com"
SUBMIT_MOCK_ENV = "CDKR_SUBMIT_MOCK"  # "1" with a local backend: 确认提交 goes to /v1/submissions
REDEEM, RELOAD = "redeem", "reload"
BUTTON_TEXT = {REDEEM: "确认提交", RELOAD: "重新读取"}
ERROR_KINDS = {flow.INVALID_FORMAT, flow.CDK_INVALID, flow.CDK_USED, flow.CDK_EXPIRED, flow.FULFILLMENT_FAILED,
               flow.NOT_SENT, flow.UNKNOWN, flow.INVALID_ACCOUNT, flow.RATE_LIMITED, flow.SERVER_ERROR,
               flow.BAD_RESPONSE, flow.NOT_READY}
_SAY = 0  # queue key for progress lines; callbacks use keys 1, 2, …

FAMILY = "Microsoft YaHei UI"      # 小助手 ships HarmonyOS Sans SC; we stay dependency-free
MAX_H = 560                        # the aurora is painted once this tall and cropped by the window
AURORA_SCALE = 2                   # computed at half size, then zoomed: a smooth gradient hides it
INK, MUTED, FAINT = paint.INK, tint(paint.INK, .68), tint(paint.INK, .45)
COLORS = {"info": MUTED, "ok": paint.OK_GREEN, "error": paint.BAD_RED}
WELL_BG, WELL_EDGE = "#fbfdff", hexc(tint(paint.CHIP_GREY, .38))
BTN_BLUE, BTN_HOT, BTN_OFF = (0, 96, 223), (0, 80, 189), tint(paint.CHIP_GREY, .45)
GUIDE_BG = hexc(tint(paint.PIN_BLUE, .08, (255, 255, 255)))
_ROWS: dict = {}                   # (level, w, h) -> aurora rows; a PhotoImage cannot cross Tk roots
_GCL_STYLE, _CS_DROPSHADOW = -26, 0x00020000


def _work(fn, key, fallback, results: queue.Queue) -> None:
    """Worker thread body. It holds no Tk object, so nothing Tk is ever released on this thread."""
    try:
        result = fn()
    except Exception:  # noqa: BLE001 - never leave the window stuck in "busy"
        result = fallback
    results.put((key, result))


def send_submission(cfg, log_path, text: str) -> flow.Outcome:
    """确认提交 on the /v1/submissions path, worded like flow.redeem. Only fixed words reach the log."""
    value = api.submit_code(text)
    if value is None:
        return flow.Outcome(flow.INVALID_FORMAT, "格式不对：请填写订单号或兑换码（4–64 位字母、数字或 -）。"
                                                 "登录信息、令牌等不要填在这里。")
    try:
        client = api.Api(cfg.base_url, timeout=cfg.timeout)
    except api.InsecureUrl as exc:
        return flow.Outcome(flow.SERVER_ERROR, str(exc))
    shown, status, started = flow.mask(value), None, time.monotonic()
    try:
        client.submit(value)
        outcome = flow.Outcome(flow.SUCCEEDED, f"已收到 {shown}。")
    except api.NotSent as exc:
        outcome = flow.Outcome(flow.NOT_SENT, "提交接口尚未开放，内容没有发出。" if exc.kind == "disabled"
                               else "网络连接失败，提交请求没有发出。请检查网络后重试。")
    except api.Unknown:
        outcome = flow.Outcome(flow.UNKNOWN, "网络中断，提交结果没有确认，请稍后重试。")
    except api.HttpError as exc:
        status = exc.status
        if exc.status == 400:
            outcome = flow.Outcome(flow.CDK_INVALID, f"{shown} 无效，请检查是否输错。")
        elif exc.status == 429:
            outcome = flow.Outcome(flow.RATE_LIMITED, "操作太频繁，请稍后再试。")
        else:
            outcome = flow.Outcome(flow.SERVER_ERROR, f"提交服务暂时不可用（{exc.status}），请稍后重试。")
    except api.BadResponse as exc:
        status = exc.status
        outcome = flow.Outcome(flow.BAD_RESPONSE, "提交服务返回了无法识别的结果，请稍后重试。")
    log.write(log_path, "submit", outcome.kind, status, int((time.monotonic() - started) * 1000))
    return outcome


def hwnd_of(root) -> int:
    """The real top-level HWND: Tk puts the widget in a wrapper window, so prefer its parent."""
    import ctypes
    handle = root.winfo_id()
    return ctypes.windll.user32.GetParent(handle) or handle


def region_height(root) -> int:
    """How tall Windows actually clips the card (SetWindowRgn); 0 when it has no region."""
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

    rgn = ctypes.windll.gdi32.CreateRectRgn(0, 0, 0, 0)
    try:
        if not ctypes.windll.user32.GetWindowRgn(hwnd_of(root), rgn):
            return 0
        box = RECT()
        ctypes.windll.gdi32.GetRgnBox(rgn, ctypes.byref(box))
        return box.b - box.t
    finally:
        ctypes.windll.gdi32.DeleteObject(rgn)


def aurora_photo(canvas: tk.Canvas, level: str, width: int, height: int) -> tk.PhotoImage:
    key = (level, width // AURORA_SCALE, height // AURORA_SCALE)
    rows = _ROWS.get(key)
    if rows is None:
        rows = _ROWS[key] = paint.aurora_rows(level, *key[1:])
    photo = tk.PhotoImage(master=canvas, width=key[1], height=key[2])
    photo.put(" ".join("{" + " ".join(row) + "}" for row in rows))
    return photo.zoom(AURORA_SCALE)


class RoundButton:
    """A rounded button drawn on the canvas. Quacks like a Tk button for `invoke` and `["text"]`."""

    def __init__(self, canvas: tk.Canvas, command, radius=None, fill=BTN_BLUE, hot=BTN_HOT,
                 ink=(255, 255, 255), font=(FAMILY, 10, "bold"), pad=18, height=36, text=""):
        self.canvas, self.command, self.radius = canvas, command, radius
        self.fill, self.hot, self.ink, self.pad, self.height = fill, hot, ink, pad, height
        self._state, self._hover = "normal", False
        self.shape = canvas.create_polygon(0, 0, 0, 0, fill=hexc(fill), outline="", smooth=False)
        self.label = canvas.create_text(0, 0, text=text, font=font, fill=hexc(ink), anchor="center")
        for item in (self.shape, self.label):
            canvas.tag_bind(item, "<Button-1>", lambda _e: self.invoke())
            canvas.tag_bind(item, "<Enter>", lambda _e: self._set_hover(True))
            canvas.tag_bind(item, "<Leave>", lambda _e: self._set_hover(False))
        self.width = 0

    def __getitem__(self, key: str):
        return self._state if key == "state" else self.canvas.itemcget(self.label, "text")

    def invoke(self) -> None:
        if self._state == "normal":
            self.command()

    def configure(self, text=None, state=None) -> None:
        if text is not None:
            self.canvas.itemconfigure(self.label, text=text)
        if state is not None:
            self._state = state
        self._repaint()

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self.canvas.configure(cursor="hand2" if hover and self._state == "normal" else "")
        self._repaint()

    def _repaint(self) -> None:
        off = self._state != "normal"
        fill = BTN_OFF if off else (self.hot if self._hover else self.fill)
        self.canvas.itemconfigure(self.shape, fill=hexc(fill))
        self.canvas.itemconfigure(self.label, fill=hexc(tint(self.ink, .75, BTN_OFF) if off else self.ink))

    def place(self, right: float, top: float) -> tuple:
        """Right-align the button; returns its box."""
        text = self.canvas.itemcget(self.label, "text")
        self.width = max(96, self.canvas.bbox(self.label)[2] - self.canvas.bbox(self.label)[0] + 2 * self.pad) \
            if text else 0
        box = (right - self.width, top, right, top + self.height)
        self._put(box)
        return box

    def _put(self, box: tuple) -> None:
        radius = self.radius if self.radius is not None else (box[3] - box[1]) / 2
        self.canvas.coords(self.shape, *round_rect_points(*box, radius))
        self.canvas.coords(self.label, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        self._repaint()


class Chip:
    """The small pill the 小助手 card uses for tags (ball_render._chip)."""

    def __init__(self, canvas: tk.Canvas, fill=paint.PIN_BLUE, ink=paint.NOTE_BLUE):
        self.canvas = canvas
        self.shape = canvas.create_polygon(0, 0, 0, 0, fill=hexc(tint(fill, .28)), outline="")
        self.label = canvas.create_text(0, 0, text="", font=(FAMILY, 8), fill=hexc(ink), anchor="center")

    def set(self, text: str) -> None:
        self.canvas.itemconfigure(self.label, text=text)
        for item in (self.shape, self.label):
            self.canvas.itemconfigure(item, state="normal" if text else "hidden")

    def place(self, right: float, middle: float) -> float:
        """Right-align the pill around `middle`; returns its left edge."""
        if not self.canvas.itemcget(self.label, "text"):
            return right
        x0, _, x1, _ = self.canvas.bbox(self.label)
        width, height = (x1 - x0) + 16, 19
        box = (right - width, middle - height / 2, right, middle + height / 2)
        self.canvas.coords(self.shape, *round_rect_points(*box, height / 2))
        self.canvas.coords(self.label, (box[0] + box[2]) / 2, middle)
        return box[0]


class App:
    def __init__(self, recharge, local_backend: bool, run_async=None, submit=None):
        self.flow = recharge
        self._submit = submit  # text -> Outcome: the mock-only submission path; None redeems
        self.mode = RELOAD
        self._threaded = run_async is None  # tests pass a synchronous runner
        self._run_async = run_async or self._thread_run
        self._results: queue.Queue = queue.Queue()
        self._callbacks: dict = {}  # key -> main-thread callback for a running worker
        self._seq = _SAY
        self._busy = False
        self._height = 0
        self._drag = (0, 0)
        self._shadowed = False
        self._shaped = 0

        self.root = tk.Tk()
        self.root.title(TITLE)
        self.root.overrideredirect(True)   # the card draws its own frame
        self.root.configure(bg=hexc(paint.CARD_BG))
        self.canvas = tk.Canvas(self.root, width=CARD_W, height=MAX_H, highlightthickness=0, bd=0,
                                bg=hexc(paint.CARD_BG))
        self.canvas.pack(fill="both", expand=True)

        self.account_var = tk.StringVar()
        self.cdk_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.footer_var = tk.StringVar(value="CDK 只在本窗口输入，不会进入 Codex 对话。"
                                             + ("（当前连接的是本机模拟后台）" if local_backend else ""))
        self._build()
        self._set_busy(True)

    # ---- the card -------------------------------------------------------------------------------

    def _build(self) -> None:
        c, inner_right = self.canvas, CARD_W - PAD_X
        self._background = aurora_photo(c, "healthy", CARD_W, MAX_H)
        c.create_image(0, 0, image=self._background, anchor="nw")
        c.bind("<Button-1>", self._drag_start)
        c.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<Escape>", lambda _e: self._on_close())
        self.root.bind("<Configure>", self._on_configure)

        c.create_text(PAD_X, PAD_TOP + 12, text="C O D E X", font=(FAMILY, 9, "bold"), fill=hexc(INK), anchor="w")
        c.create_text(PAD_X + 74, PAD_TOP + 12, text="充值", font=(FAMILY, 9), fill=hexc(MUTED), anchor="w")
        self.close_button = RoundButton(c, self._on_close, fill=(255, 255, 255), hot=(236, 242, 248),
                                        ink=INK, font=(FAMILY, 9), height=24, text="✕")
        self.close_button._put((inner_right - 24, PAD_TOP, inner_right, PAD_TOP + 24))
        self.live_dot = c.create_oval(0, 0, 0, 0, fill=hexc(paint.STATUS_STALE), outline="")
        # the body keeps its spacing, pushed down by however tall the guide panel came out
        top = self._top = self._build_guide(PAD_TOP + 40) - 24

        c.create_text(PAD_X, top + 50, text="充值账号", font=(FAMILY, 9), fill=hexc(FAINT), anchor="w")
        account_well = (PAD_X, top + 66, inner_right, top + 104)
        c.create_polygon(*round_rect_points(*account_well, 11), fill=WELL_BG, outline=WELL_EDGE, width=1)
        self.account_entry = tk.Entry(c, textvariable=self.account_var, font=(FAMILY, 11), bd=0,
                                      relief="flat", bg=WELL_BG, fg=hexc(INK),
                                      disabledbackground=WELL_BG, disabledforeground=hexc(FAINT),
                                      insertbackground=hexc(INK), highlightthickness=0, justify="left")
        self.account_entry.bind("<Return>", lambda _e: self.entry.focus_set())
        c.create_window(account_well[0] + 13, (account_well[1] + account_well[3]) / 2,
                        window=self.account_entry, anchor="w",
                        width=account_well[2] - account_well[0] - 26, height=22)

        c.create_text(PAD_X, top + 116, text="CDK", font=(FAMILY, 9), fill=hexc(FAINT), anchor="w")
        well = (PAD_X, top + 130, inner_right, top + 168)
        c.create_polygon(*round_rect_points(*well, 11), fill=WELL_BG, outline=WELL_EDGE, width=1)
        self.entry = tk.Entry(c, textvariable=self.cdk_var, font=(FAMILY, 11), bd=0, relief="flat",
                              bg=WELL_BG, fg=hexc(INK), disabledbackground=WELL_BG,
                              disabledforeground=hexc(FAINT), insertbackground=hexc(INK),
                              highlightthickness=0, justify="left")
        self.entry.bind("<Return>", lambda _e: self._on_return())
        c.create_window(well[0] + 13, (well[1] + well[3]) / 2, window=self.entry, anchor="w",
                        width=well[2] - well[0] - 26, height=22)

        self.button = RoundButton(c, self._on_click, text=BUTTON_TEXT[RELOAD])
        self.account_var.trace_add("write", lambda *_args: self.button.configure(state=self._button_state()))
        self.cdk_var.trace_add("write", lambda *_args: self.button.configure(state=self._button_state()))
        self.status_text = self._text_item(PAD_X, 0, self.status_var, font=(FAMILY, 9), fill=MUTED,
                                           anchor="nw", width=CARD_W - 2 * PAD_X)
        self.footer_text = self._text_item(PAD_X, 0, self.footer_var, font=(FAMILY, 8), fill=FAINT,
                                           anchor="nw", width=CARD_W - 2 * PAD_X)
        self._relayout()

    def _build_guide(self, top: float) -> float:
        """The 操作说明 panel: a hint and a link that opens in the default browser. Returns its bottom."""
        c, inner_right = self.canvas, CARD_W - PAD_X
        panel = c.create_polygon(0, 0, 0, 0, fill=GUIDE_BG, outline="")
        width = inner_right - PAD_X - 28
        hint = c.create_text(PAD_X + 14, top + 10, text=GUIDE_TEXT, font=(FAMILY, 9), fill=hexc(INK),
                             anchor="nw", width=width)
        self.link = c.create_text(PAD_X + 14, c.bbox(hint)[3] + 4, text=RECHARGE_URL,
                                  font=(FAMILY, 9, "underline"), fill=hexc(BTN_BLUE), anchor="nw", width=width)
        c.tag_bind(self.link, "<Button-1>", lambda _e: self._open_link())
        c.tag_bind(self.link, "<Enter>", lambda _e: self._hover_link(True))
        c.tag_bind(self.link, "<Leave>", lambda _e: self._hover_link(False))
        bottom = c.bbox(self.link)[3] + 10
        c.coords(panel, *round_rect_points(PAD_X, top, inner_right, bottom, 11))
        return bottom

    def _hover_link(self, hover: bool) -> None:
        self.canvas.configure(cursor="hand2" if hover else "")
        self.canvas.itemconfigure(self.link, fill=hexc(BTN_HOT if hover else BTN_BLUE))

    def _open_link(self) -> None:
        webbrowser.open(RECHARGE_URL)

    def _text_item(self, x: float, y: float, var: tk.StringVar, fill, **kw) -> int:
        """A canvas text item that follows a StringVar (canvas items have no -textvariable)."""
        item = self.canvas.create_text(x, y, text=var.get(), fill=hexc(fill), **kw)

        def follow(*_args) -> None:
            self.canvas.itemconfigure(item, text=var.get())
            self._relayout()

        var.trace_add("write", follow)
        return item

    def _relayout(self) -> None:
        """Place everything below the CDK box: the card grows with however long the status is."""
        c, inner_right = self.canvas, CARD_W - PAD_X
        middle = self._top + 74
        dot = self.close_button.canvas.coords(self.close_button.label)[0] - 24
        c.coords(self.live_dot, dot - 4, PAD_TOP + 8, dot + 4, PAD_TOP + 16)

        y = self.button.place(inner_right, self._top + 182)[3] + 18
        c.coords(self.status_text, PAD_X, y)
        if self.status_var.get():
            y = c.bbox(self.status_text)[3] + 14
        c.coords(self.footer_text, PAD_X, y)
        height = round(c.bbox(self.footer_text)[3] + PAD_BOTTOM)
        if height != self._height:
            self._height = height
            self.root.geometry(f"{CARD_W}x{height}")
            self.canvas.configure(height=height)

    def _on_configure(self, event) -> None:
        if event.widget is self.root and event.height != self._shaped:
            self._shaped = event.height
            self._round_corners(event.height)

    def _round_corners(self, height: int) -> None:
        if self.root.tk.call("tk", "windowingsystem") != "win32":
            return
        try:
            import ctypes
            hwnd = hwnd_of(self.root)
            radius = round(paint.CARD_RADIUS * 2)
            region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, CARD_W + 1, height + 1, radius, radius)
            ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
            if not self._shadowed:  # CS_DROPSHADOW: the frame Windows draws for borderless popups
                self._shadowed = True
                style = ctypes.windll.user32.GetClassLongW(hwnd, _GCL_STYLE)
                ctypes.windll.user32.SetClassLongW(hwnd, _GCL_STYLE, style | _CS_DROPSHADOW)
        except (AttributeError, OSError, tk.TclError):
            pass  # square corners and no shadow are a cosmetic loss, not a failure

    # ---- lifecycle ------------------------------------------------------------------------------

    def start(self) -> None:
        self._say("请输入充值账号和 CDK。", "info")
        self._run_async(self.flow.load, self._show_view)

    def run(self) -> None:
        self._center()
        self.root.after(50, self._bring_to_front)
        self.root.after(100, self._drain)
        self.start()
        self.root.mainloop()

    # ---- actions --------------------------------------------------------------------------------

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        self.root.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _on_return(self) -> None:
        if self.mode == REDEEM and self.button["state"] != "disabled":
            self._on_click()

    def _on_click(self) -> None:
        if self.mode == RELOAD:
            self._set_busy(True)
            self.start()
        else:
            recharge, account, cdk, submit = self.flow, self.account_var.get(), self.cdk_var.get(), self._submit
            self._set_busy(True)
            self._say("正在提交…", "info")
            if submit:
                self._run_async(lambda: submit(cdk), self._show_outcome)
                return
            progress = self._progress_sink()
            self._run_async(lambda: recharge.redeem(cdk, account=account, progress=progress), self._show_outcome)

    def _on_close(self) -> None:
        if self._busy and self.mode == REDEEM and not messagebox.askyesno(
                TITLE, "正在兑换，现在关闭将看不到结果。\n之后再次提交同一 CDK 即可查询结果，不会重复扣除。\n\n仍要关闭吗？",
                parent=self.root):
            return
        self.root.destroy()

    # ---- results --------------------------------------------------------------------------------

    def _show_view(self, view: flow.View) -> None:
        if view.email:
            self.account_var.set(view.email)
        ready = view.state == flow.READY
        self.mode = REDEEM if ready else RELOAD
        self.canvas.itemconfigure(self.live_dot, fill=hexc(paint.STATUS_OK if ready else paint.STATUS_STALE))
        self._say(view.message, "error" if view.state in (flow.NOT_CONFIGURED, flow.NO_ACCOUNT) else "info")
        self._set_busy(False)

    def _show_outcome(self, outcome: flow.Outcome) -> None:
        if outcome.ok:
            self.cdk_var.set("")
        tone = "ok" if outcome.ok else ("error" if outcome.kind in ERROR_KINDS else "info")
        self._say(f"提交成功。{outcome.message}" if outcome.ok else outcome.message, tone)
        self._set_busy(False)

    def _append_note(self, shown: str, note: str) -> None:
        if note and self.status_var.get() == shown:  # not if another action replaced the message
            self.status_var.set(shown + note)
            self._relayout()

    # ---- helpers --------------------------------------------------------------------------------

    def _say(self, text: str, tone: str) -> None:
        self.status_var.set(text)
        self.canvas.itemconfigure(self.status_text, fill=hexc(COLORS[tone]))
        self._relayout()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.button.configure(text=BUTTON_TEXT[self.mode], state=self._button_state())
        state = "normal" if (self.mode == REDEEM and not busy) else "disabled"
        self.account_entry.configure(state=state)
        self.entry.configure(state=state)
        if not busy and self.mode == REDEEM:
            self.entry.focus_set()
        self._relayout()

    def _button_state(self) -> str:
        """Off while a request runs (no second submit), and while there is no CDK to redeem."""
        blank = self.mode == REDEEM and (not self.account_var.get().strip() or not self.cdk_var.get().strip())
        return "disabled" if self._busy or blank else "normal"

    def _progress_sink(self):
        if not self._threaded:
            return lambda msg: self._say(msg, "info")
        results = self._results  # called on the worker thread: only the queue, never Tk
        return lambda msg: results.put((_SAY, msg))

    def _thread_run(self, fn, done) -> None:
        if done == self._show_view:
            fallback = flow.View(flow.NO_ACCOUNT, "出现意外错误，请点「重新读取」。")
        elif done == self._show_outcome:
            fallback = flow.Outcome(flow.SERVER_ERROR, "出现意外错误，请关闭窗口后重试。")
        else:
            fallback = ""
        self._seq += 1
        self._callbacks[self._seq] = done
        threading.Thread(target=_work, args=(fn, self._seq, fallback, self._results), daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                key, result = self._results.get_nowait()
                if key == _SAY:
                    self._say(result, "info")
                elif key in self._callbacks:
                    self._callbacks.pop(key)(result)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain)

    def _center(self) -> None:
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - CARD_W) // 2
        y = (self.root.winfo_screenheight() - self._height) // 3
        self.root.geometry(f"+{x}+{y}")

    def _bring_to_front(self) -> None:
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(400, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()


def main() -> int:
    import ctypes
    import functools
    import gc
    import os

    from . import api, config, log, single

    if os.name == "nt":
        handle = single.acquire()
        if handle is None:
            single.raise_existing(TITLE)
            return 0
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    paths = config.Paths.default()
    cfg = config.load(paths)
    submit = None
    if os.environ.get(SUBMIT_MOCK_ENV) == "1" and api.is_local(cfg.base_url):
        submit = functools.partial(send_submission, cfg, paths.log)
    try:
        app = App(flow.Recharge(paths, cfg), local_backend=api.is_local(cfg.base_url),
                  submit=submit)
        app.run()
    except Exception:  # noqa: BLE001
        log.write(paths.log, "window", "crashed")
        return 1
    finally:
        app = None
        gc.collect()  # free Tk on the main thread, not on a worker that happens to run the GC
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
