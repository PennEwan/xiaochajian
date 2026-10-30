"""End-to-end: MCP tool call → native CDK window → mock backend → CDK-free result back to the tool.

Runs assist.exe exactly as Codex does (stdio MCP), calls `open_recharge_window`, and drives the
real WinForms window through Windows UI Automation (PowerShell, no extra packages). The account
comes from the real `account/read` of the App kernel; the backend is the in-process mock; all
plugin data goes to a throwaway directory. Screenshots: .testdata/native-window/.

    python scripts\\native_window_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from cdkr_helpers import MockBackend  # noqa: E402

EXE = REPO / "plugins" / "codex-native-assist" / "bin" / "assist.exe"
OUT = REPO / ".testdata" / "native-window"
TITLE = "Codex 充值"

# ---- driving the real window through plain Win32 messages (Unicode-safe, no extra packages) ----
import ctypes  # noqa: E402
from ctypes import wintypes  # noqa: E402

U = ctypes.WinDLL("user32", use_last_error=True)
ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
U.SendMessageW.restype = ctypes.c_ssize_t
U.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p]
WM_GETTEXT, WM_GETTEXTLENGTH, WM_SETTEXT, BM_CLICK = 0x000D, 0x000E, 0x000C, 0x00F5


def _text(h) -> str:
    n = U.SendMessageW(h, WM_GETTEXTLENGTH, 0, None)
    buf = ctypes.create_unicode_buffer(n + 1)
    U.SendMessageW(h, WM_GETTEXT, n + 1, ctypes.cast(buf, ctypes.c_void_p))
    return buf.value


def _class(h) -> str:
    buf = ctypes.create_unicode_buffer(256)
    U.GetClassNameW(h, buf, 256)
    return buf.value


CONSENT_TITLE = "Codex 原生助手 · 任务记录授权"
CURRENT = {"title": TITLE}


def window():
    found = []

    def cb(h, _):
        if U.IsWindowVisible(h) and _text(h) == CURRENT["title"] and _class(h).startswith("WindowsForms10"):
            found.append(h)
        return True

    U.EnumWindows(ENUM(cb), 0)
    return found[0] if found else None


def controls() -> list[dict]:
    w = window()
    if not w:
        return []
    out = []

    def cb(h, _):
        cls = _class(h)
        kind = "button" if ".BUTTON." in cls else "edit" if ".EDIT." in cls else "text" if ".STATIC." in cls else cls
        out.append({"h": h, "kind": kind, "text": _text(h), "visible": bool(U.IsWindowVisible(h)),
                    "enabled": bool(U.IsWindowEnabled(h))})
        return True

    U.EnumChildWindows(w, ENUM(cb), 0)
    return out


def texts() -> list[str]:
    return [c["text"] for c in controls() if c["kind"] == "text" and c["visible"] and c["text"]]


def button_ready(name: str) -> bool:
    return any(c["kind"] == "button" and c["text"] == name and c["visible"] and c["enabled"] for c in controls())


def click(name: str) -> bool:
    for c in controls():
        if c["kind"] == "button" and c["text"] == name and c["visible"] and c["enabled"]:
            U.PostMessageW(c["h"], BM_CLICK, 0, 0)
            return True
    return False


def type_cdk(value: str) -> bool:
    for c in controls():
        if c["kind"] == "edit" and c["visible"] and c["enabled"]:
            buf = ctypes.create_unicode_buffer(value)
            U.SendMessageW(c["h"], WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p))
            return True
    return False


def shot(path: Path) -> None:
    w = window()
    if not w:
        return
    r = wintypes.RECT()
    U.GetWindowRect(w, ctypes.byref(r))
    ps = (f"Add-Type -AssemblyName System.Drawing; $b = New-Object System.Drawing.Bitmap {r.right - r.left},{r.bottom - r.top}; "
          f"$g = [System.Drawing.Graphics]::FromImage($b); $g.CopyFromScreen({r.left},{r.top},0,0,$b.Size); "
          f"$b.Save('{path}'); $g.Dispose(); $b.Dispose()")
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True)


def wait(pred, timeout: float = 40, step: float = 0.3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


class Mcp:
    """assist.exe over stdio, the way Codex runs it. Keeps every byte it sends for the leak check."""

    def __init__(self, env: dict):
        self.p = subprocess.Popen([str(EXE), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.raw: list[bytes] = []
        self.responses: dict[int, dict] = {}
        self.cond = threading.Condition()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.call({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}},
                  method="initialize")

    def _read(self) -> None:
        for line in self.p.stdout:
            self.raw.append(line)
            msg = json.loads(line)
            with self.cond:
                self.responses[msg.get("id")] = msg
                self.cond.notify_all()

    def call(self, params: dict, method: str = "tools/call", timeout: float = 300) -> dict:
        with self.cond:
            self.next_id += 1
            rid = self.next_id
            line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"
            self.p.stdin.write(line.encode())
            self.p.stdin.flush()
            if not self.cond.wait_for(lambda: rid in self.responses, timeout):
                raise TimeoutError(method)
            return self.responses.pop(rid)

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait(10)


def tool_async(mcp: Mcp, name: str, args: dict) -> dict:
    box: dict = {}

    def run():
        box["result"] = mcp.call({"name": name, "arguments": args})["result"]

    t = threading.Thread(target=run, daemon=True)
    t.start()
    box["thread"] = t
    return box


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="nat-window-"))
    backend = MockBackend()
    env = dict(os.environ, ASSIST_DATA_DIR=str(data), ASSIST_RECHARGE_URL=backend.url,
               ASSIST_RECORDING_DEFAULT="off")  # release behaviour: off until consent
    env.pop("CODEX_HOME", None)  # account/read uses the real login (read-only)
    results: list[dict] = []
    mcp = Mcp(env)
    try:
        # 1. first use: bind, redeem a valid CDK, close → tool returns success without the CDK
        call = tool_async(mcp, "open_recharge_window", {})
        assert wait(window, 20), "window did not appear"
        assert wait(lambda: button_ready("确认充值到此账号"), 40), f"bind button never ready: {texts()}"
        shot(OUT / "1-bind.png")
        click("确认充值到此账号")
        assert wait(lambda: any("绑定成功" in t for t in texts()), 20), texts()
        type_cdk("TEST-OK01")
        assert wait(lambda: button_ready("确认兑换"), 10)
        click("确认兑换")
        ok = wait(lambda: any("充值成功" in t for t in texts()), 30)
        shot(OUT / "2-success.png")
        shown = [t for t in texts() if "充值成功" in t]
        click("关闭")
        call["thread"].join(30)
        res = call["result"]
        results.append({"case": "bind + valid CDK", "window_message": shown, "tool_text": res["content"][0]["text"],
                        "tool_structured": res.get("structuredContent"), "ok": bool(ok) and not res["isError"]})

        # 2. invalid CDK, then a second launch while open (single instance), then close
        call = tool_async(mcp, "open_recharge_window", {})
        assert wait(lambda: button_ready("关闭") and any("已绑定" in t for t in texts()), 40), texts()
        type_cdk("TEST-NOPE")
        click("确认兑换")
        bad = wait(lambda: [t for t in texts() if "无效" in t], 20)
        second = mcp.call({"name": "open_recharge_window", "arguments": {"wait": False}})["result"]
        shot(OUT / "3-invalid.png")
        click("关闭")
        call["thread"].join(30)
        res = call["result"]
        results.append({"case": "invalid CDK", "window_message": bad, "tool_text": res["content"][0]["text"],
                        "second_launch": second["content"][0]["text"],
                        "ok": bool(bad) and res["structuredContent"]["kind"] == "cdk_invalid"
                        and second["structuredContent"]["status"] == "already_open"})

        # 3. open and close without redeeming
        call = tool_async(mcp, "open_recharge_window", {})
        assert wait(lambda: button_ready("关闭") and any("已绑定" in t for t in texts()), 40)
        click("关闭")
        call["thread"].join(30)
        res = call["result"]
        results.append({"case": "cancel", "tool_text": res["content"][0]["text"],
                        "ok": res["structuredContent"]["status"] == "cancelled"})

        # 4. consent: only the user's tick in the window turns recording on; pause_recording turns it off
        CURRENT["title"] = CONSENT_TITLE
        call = tool_async(mcp, "open_recording_settings", {})
        assert wait(window, 20), "consent window did not appear"
        wait(lambda: button_ready("关闭窗口"), 10)
        enable_before = button_ready("开启记录")
        click("我已阅读以上说明，同意按所选范围记录")
        enable_after = wait(lambda: button_ready("开启记录"), 5)
        click("开启记录")
        on = wait(lambda: [t for t in texts() if t.startswith("当前：已开启")], 10)
        shot(OUT / "4-consent.png")
        click("关闭窗口")
        call["thread"].join(30)
        res = call["result"]
        paused = mcp.call({"name": "pause_recording", "arguments": {}})["result"]
        results.append({"case": "consent window + pause", "enable_disabled_before_tick": not enable_before,
                        "enable_after_tick": bool(enable_after), "window_state": on, "tool_text": res["content"][0]["text"],
                        "pause_text": paused["content"][0]["text"],
                        "ok": not enable_before and bool(enable_after) and bool(on)
                        and res["structuredContent"]["enabled"] is True and paused["structuredContent"]["enabled"] is False})
        CURRENT["title"] = TITLE

        # 5. the CDK must not appear anywhere outside the window process
        mcp.close()
        blobs = {"mcp_stdout": b"".join(mcp.raw).decode("utf-8", "replace")}
        for f in data.rglob("*"):
            if f.is_file() and f.suffix != ".bin":
                blobs[str(f.relative_to(data))] = f.read_text(encoding="utf-8", errors="replace")
        leaks = {k: [c for c in ("TEST-OK01", "TEST-NOPE") if c in v] for k, v in blobs.items()}
        leaks = {k: v for k, v in leaks.items() if v}
        backend_paths = list(backend.state.paths)
        results.append({"case": "CDK leak check", "checked": sorted(blobs), "leaks": leaks,
                        "backend_paths": backend_paths, "ok": not leaks and all("TEST" not in p for p in backend_paths)})
    finally:
        if mcp.p.poll() is None:
            mcp.p.kill()
        backend.stop()
        shutil.rmtree(data, ignore_errors=True)
    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        print(("PASS " if r["ok"] else "FAIL ") + json.dumps(r, ensure_ascii=False))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
