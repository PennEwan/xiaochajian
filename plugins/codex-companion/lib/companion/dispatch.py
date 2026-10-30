"""The plugin's single hook entry: every Codex lifecycle event runs hooks/hook.py -> main().

One Python process per event (P1 and P3 each started their own for every prompt).

UserPromptSubmit:
  /充值 …   cdkr.trigger opens the recharge window and blocks the prompt. A blocked prompt goes
            neither to the model nor to the recorder, so a CDK typed after the command is never
            written anywhere.
  /记录 …   opens the control panel and blocks the prompt.
  otherwise the recorder (ctr.hook.handle) with this bundle's wording.
Then, in user sessions: once a day an available update is mentioned, and a due update check is
started in the background — the hook itself never touches the network.

Contract with Codex: always exit 0; print nothing but one JSON object.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import local, log, update
from .product import Product, load

PANEL_COMMANDS = ("/记录", "／记录")  # full-width slash: Chinese IME in full-width mode
RECHARGE_COMMANDS = ("/充值", "／充值")
REASON_PANEL = "已打开「Codex 助手包」控制面板，这条消息没有发给模型。"
REASON_PANEL_FAILED = "控制面板没能打开，请从开始菜单打开「Codex 助手包」。这条消息没有发给模型。"
REASON_RECHARGE_FAILED = "充值窗口没能打开，请从开始菜单打开「Codex 助手包 → Codex 充值」。这条消息没有发给模型。"
REASON_BROKEN = "Codex 助手包出了问题，这条消息已拦下、没有发给模型。请从开始菜单打开「Codex 助手包」查看。"
NOTICES = {
    "syncing": "● Codex 助手包正在记录本会话并上传（你已授权）。查看或暂停：发送 /记录，或打开开始菜单「Codex 助手包」",
    "recording": "● Codex 助手包正在本机记录本会话（当前未上传）。查看或暂停：发送 /记录",
    "not_enabled": None,  # recharge-only users are never nagged about recording
}


def is_panel_command(prompt: object) -> bool:
    return isinstance(prompt, str) and prompt.lstrip().startswith(PANEL_COMMANDS)


def _default_panel() -> bool:
    from .launch import open_panel
    return open_panel()


def _merge(out: dict | None, line: str) -> dict:
    out = dict(out or {})
    out["systemMessage"] = f"{out['systemMessage']}\n{line}" if out.get("systemMessage") else line
    return out


def handle(raw: bytes, *, product: Product, cpaths: local.Paths, rpaths, env, started: float,
           open_recharge=None, open_panel=None) -> dict | None:
    payload = json.loads(raw.decode("utf-8", "replace")) if raw else None
    if not isinstance(payload, dict):
        return None
    event = str(payload.get("hook_event_name") or "?")
    if event == "UserPromptSubmit":
        from cdkr import trigger

        blocked = trigger.handle(raw, open_recharge)
        if blocked:
            if blocked.get("reason") == trigger.REASON_LAUNCH_FAILED:  # names 充值.cmd, which the bundle lacks
                blocked["reason"] = REASON_RECHARGE_FAILED
            return blocked
        if is_panel_command(payload.get("prompt")):
            try:
                opened = bool((open_panel or _default_panel)())
            except Exception:  # noqa: BLE001 - the prompt is still blocked
                opened = False
            return {"decision": "block", "reason": REASON_PANEL if opened else REASON_PANEL_FAILED}
    out = None
    try:
        from ctr import hook

        out = hook.handle(payload, rpaths, env, started, notices=NOTICES)
    except Exception as exc:  # noqa: BLE001 - recording is never worth breaking a session
        log.write(cpaths.log, "hook", type(exc).__name__.lower(), event=_safe(event))
    if event == "UserPromptSubmit" and payload.get("transcript_path"):  # a user's session, not a background task
        try:
            note = update.notice(product, cpaths)
            if note:
                out = _merge(out, note)
        except Exception as exc:  # noqa: BLE001 - never at the cost of the recording notice
            log.write(cpaths.log, "update_notice", type(exc).__name__.lower())
        update.kick(product, cpaths)
    return out


def command_block(raw: bytes) -> dict | None:
    """Last resort when handling failed: still keep /充值… (it may carry a CDK) and /记录 from the model."""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
        prompt = payload.get("prompt") if isinstance(payload, dict) else None
        if (payload.get("hook_event_name") == "UserPromptSubmit" and isinstance(prompt, str)
                and prompt.lstrip().startswith(RECHARGE_COMMANDS + PANEL_COMMANDS)):
            return {"decision": "block", "reason": REASON_BROKEN}
    except Exception:  # noqa: BLE001
        pass
    return None


def _safe(event: str) -> str:
    return event.lower() if event.isalpha() else "unknown"


def main(stdin=None, stdout=None, env=None, rpaths=None, cpaths=None, product=None) -> int:
    started = time.perf_counter()
    cpaths = cpaths or local.Paths.default()
    raw = b""
    out = None
    try:
        raw = (stdin if stdin is not None else sys.stdin.buffer).read()
        from ctr.paths import Paths as RecorderPaths

        out = handle(raw, product=product or load(), cpaths=cpaths, rpaths=rpaths or RecorderPaths.default(),
                     env=os.environ if env is None else env, started=started)
    except BaseException as exc:  # noqa: BLE001 - never break the user's session
        log.write(cpaths.log, "dispatch", type(exc).__name__.lower())
        out = command_block(raw)
    try:
        if out:
            stdout = stdout if stdout is not None else sys.stdout
            stdout.write(json.dumps(out, ensure_ascii=True))
            stdout.flush()
    except BaseException:  # noqa: BLE001
        pass
    return 0
