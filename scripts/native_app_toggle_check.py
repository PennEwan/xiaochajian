"""Real App: switch the plugin off and on in Settings → 插件, with a chat in between (all in project ctr-testbed).

Content sent while the plugin is off must never be collected, also not after it is switched on again;
content after re-enabling must be. Drives the UI with scripts/appui.py; verifies via the plugin store.

    python scripts\\native_app_toggle_check.py
Results: .testdata/native-desktop/toggle-check.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import appui  # noqa: E402
import native_app_multisession as M  # noqa: E402

CONFIG = Path(os.environ["USERPROFILE"]) / ".codex" / "config.toml"
OUT = REPO / ".testdata" / "native-desktop" / "toggle-check.json"


def enabled() -> str:
    text = CONFIG.read_text(encoding="utf-8")
    i = text.find('[plugins."codex-native-assist@ctr-local"]')
    return text[i:].split("\n")[1].strip()


def mcp_count() -> int:
    return len([p for p in M.procs() if p["mode"] == "mcp"])


def open_plugin_list(h):
    l, t, r, b = appui.rect(h)
    appui.click(h, l + 26, t + 245)   # 自定义 → 插件
    time.sleep(1.5)
    appui.click(h, l + 160, t + 285)  # Codex 原生助手（PoC）
    time.sleep(1.8)
    appui.click(h, l + 904, t + 200)  # 管理
    time.sleep(1.5)
    appui.click(h, l + 395, t + 67)   # 插件设置 (list with switches)
    time.sleep(1.5)


def toggle(h, shot: str) -> None:
    open_plugin_list(h)
    l, t, r, b = appui.rect(h)
    appui.screenshot(REPO / ".testdata" / "native-desktop" / "shots" / f"{shot}-before.png", (l, t, r, b))
    appui.click(h, l + 984, t + 437)  # the switch on the 「Codex 原生助手（PoC）」 row
    time.sleep(3)


def chat_in_project(h, text: str) -> None:
    l, t, r, b = appui.rect(h)
    appui.click(h, l + 26, t + 70)    # home
    time.sleep(1.2)
    appui.click(h, 126, 301)          # 新聊天
    time.sleep(1.5)
    appui.click(h, l + 436, t + 684)  # project chip
    time.sleep(1.0)
    appui.click(h, l + 450, t + 555)  # ctr-testbed
    time.sleep(1.0)
    appui.click(h, l + 700, t + 727)
    time.sleep(0.4)
    appui.type_text(h, text)
    appui.press(h, appui.VK_RETURN)


def main() -> int:
    w = appui.app_window()
    h = w["hwnd"]
    appui.bring_to_front(h)
    time.sleep(0.3)
    log = {"start": {"config": enabled(), "mcp": mcp_count()}}
    toggle(h, "toggle-off")
    log["after_off"] = {"config": enabled(), "mcp": mcp_count(), "at": datetime.now(timezone.utc).isoformat()}
    chat_in_project(h, "开关测试 D2：插件关闭时在 ctr-testbed 发送，不应被记录，只回复 ok")
    time.sleep(25)
    log["while_off"] = {"mcp": mcp_count()}
    toggle(h, "toggle-on")
    log["after_on"] = {"config": enabled(), "mcp": mcp_count(), "at": datetime.now(timezone.utc).isoformat()}
    chat_in_project(h, "开关测试 E2：重新打开后在 ctr-testbed 发送，应被记录，只回复 ok")
    time.sleep(35)
    prompts = [r[0] for r in M.q("SELECT json_extract(body, '$.data.text') FROM events WHERE kind = 'user_prompt' ORDER BY ts")]
    log["result"] = {
        "mcp": mcp_count(),
        "d2_recorded": any("开关测试 D2" in (p or "") for p in prompts),
        "e2_recorded": any("开关测试 E2" in (p or "") for p in prompts),
        "periods": M.q("SELECT * FROM consent_periods"),
        "gap_events": [json.loads(l) for l in (M.DATA / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
                       if '"consent_gap_excluded"' in l or '"consent_suspended"' in l or '"consent_resumed"' in l][-3:],
    }
    OUT.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    return 0 if (not log["result"]["d2_recorded"] and log["result"]["e2_recorded"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
