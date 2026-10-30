"""Codex hook entry point of 「CDK 充值」: every lifecycle event runs this script with the
plugin's own Python (runtime\\python.exe, see hooks.json).

It must never break the user's session: whatever happens, it exits 0. If the plugin's library
cannot be loaded, `/充值…` and `/记录…` are still blocked here (the first may carry a CDK),
using nothing but the standard library.
"""
import json
import os
import sys

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
_COMMANDS = ("/充值", "／充值", "/记录", "／记录")


def _fallback(raw: bytes) -> None:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
        prompt = payload.get("prompt") if isinstance(payload, dict) else None
        if isinstance(prompt, str) and prompt.lstrip().startswith(_COMMANDS):
            reason = "CDK 充值没能加载，这条消息已拦下、没有发给模型。请重新安装 CDK 充值。"
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=True))
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001
        pass


try:
    from companion.dispatch import main
except BaseException:  # a broken install must not break the user's session
    try:
        _fallback(sys.stdin.buffer.read())
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)

sys.exit(main())
