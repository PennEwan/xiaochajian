"""The service terms this backend advertises.

`GET /v1/service-state` returns `terms_version` + `terms_hash`, and the collector stores both in
its local `contract.json`. They must be the same text the installer showed the user — otherwise
"what was agreed" and "what the backend claims" differ.

So the text is not duplicated here: it is imported from the collector's `contract.py` when that
file is reachable (a checkout, a staged build), and only falls back to the constants below in a
standalone deployment. `tests/test_server_core.py` asserts the two never drift.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

TERMS_VERSION = 4
TERMS_TEXT = (
    "本产品为会话记录服务。"
    "本产品以低于市场价的额度提供充值，对价是你授权我们记录并上传 Codex 会话数据，"
    "含完整会话原文与运行遥测。"
    "记录：对话、模型回复、工具调用与结果、完整会话原文、时间与模型配置，"
    "以及运行遥测（会话数、工具调用次数、错误与时延）。"
    "不记录：密码、API Key、令牌、Cookie、私钥。"
    "记录由随本产品安装的后台组件提供并持续运行；在 Codex 中停用或卸载插件不影响记录。"
    "终止：控制面板「停止记录」，或「设置 → 应用 → 卸载 Codex 助手包」。"
)
GRACE_HOURS = 72


def _plugin_terms() -> tuple[int, str, str] | None:
    candidates = [
        Path(__file__).resolve().parents[2] / "plugins" / "codex-companion" / "lib" / "collector" / "contract.py",
        Path(__file__).resolve().parents[1] / "collector" / "contract.py",
    ]
    for path in candidates:
        try:
            namespace: dict = {"__name__": "collector_terms_probe"}
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)  # noqa: S102
        except (OSError, SyntaxError):
            continue
        version, text, digest = namespace.get("TERMS_VERSION"), namespace.get("TERMS_TEXT"), namespace.get("TERMS_HASH")
        if isinstance(version, int) and isinstance(text, str) and isinstance(digest, str):
            return version, text, digest
    return None


_plugin = _plugin_terms()
if _plugin:
    TERMS_VERSION, TERMS_TEXT, TERMS_HASH = _plugin
else:
    TERMS_HASH = "sha256:" + hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()


def state_document() -> dict:
    """The body of `GET /v1/service-state` (per-device fields are filled in by the handler)."""
    return {"terms_version": TERMS_VERSION, "terms_hash": TERMS_HASH, "grace_hours": GRACE_HOURS}
