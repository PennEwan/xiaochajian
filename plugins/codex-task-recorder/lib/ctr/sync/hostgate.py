"""May the uploader run? Only while Codex still runs the plugin that hosts this code.

When the user disables the plugin, switches one of its hooks off, revokes trust or uninstalls
it, Codex simply stops calling the hooks — but the uploader is a separate process and must
notice by itself. It reads the same `config.toml` Codex does:

    [plugins."<id>"] enabled = true                  missing -> uninstalled; false -> disabled
    [hooks.state."<id>:hooks/hooks.json:<event>:<group>:<hook>"]
        trusted_hash = "sha256:…"                    missing -> not trusted
        enabled = false                              the user switched this hook off

Host plugin id: `plugin_id` in the plugin root's product.json (the Codex 助手包 bundle), or
inferred from the plugin cache path …\\plugins\\cache\\<marketplace>\\<plugin>\\<version>.
Code running from a source checkout has neither and is not gated.

What it cannot see: a hook whose definition changed since it was trusted (`modified`) still
has a trusted_hash here; only Codex's own hooks/list can tell, so the control panel shows it.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 has no tomllib: no gate
    tomllib = None

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
UNINSTALLED, DISABLED = "plugin_uninstalled", "plugin_disabled"
HOOK_OFF, UNTRUSTED, UNREADABLE = "hook_disabled", "hook_untrusted", "config_unreadable"
REASON_TEXT = {
    UNINSTALLED: "插件已从 Codex 中卸载",
    DISABLED: "插件已在 Codex 中禁用",
    HOOK_OFF: "插件的 Hook 已在 Codex 设置中被关闭",
    UNTRUSTED: "插件的 Hook 尚未被信任（或信任已撤销）",
    UNREADABLE: "无法读取 Codex 配置",
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def host_plugin_id(root: Path = PLUGIN_ROOT) -> str | None:
    try:
        doc = json.loads((root / "product.json").read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("plugin_id"), str) and "@" in doc["plugin_id"]:
            return doc["plugin_id"]
    except (OSError, ValueError):
        pass
    parts = [p.lower() for p in root.parts]
    if len(parts) >= 5 and parts[-5:-3] == ["plugins", "cache"]:
        return f"{root.parts[-2]}@{root.parts[-3]}"
    return None


def hook_keys(root: Path, plugin_id: str) -> list[str]:
    """The hooks.state keys Codex uses for this plugin's hooks.

    A plugin that declares no `"hooks"` in its manifest has none — Codex never runs them, so
    there is nothing to trust and nothing to gate on. (The shipped companion plugin is like
    that: one MCP server, no hooks.) A tree without a readable manifest keeps the old default.
    """
    rel = "hooks/hooks.json"
    try:
        manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        pass
    else:
        if not isinstance(manifest.get("hooks"), str):
            return []
        rel = manifest["hooks"].replace("\\", "/").removeprefix("./")
    try:
        events = json.loads((root / rel).read_text(encoding="utf-8")).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        return []
    keys = []
    for event, groups in events.items():
        for gi, group in enumerate(groups or []):
            for hi, _ in enumerate((group or {}).get("hooks") or []):
                keys.append(f"{plugin_id}:{rel}:{_snake(event)}:{gi}:{hi}")
    return keys


def codex_home(environ=None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else Path.home() / ".codex"


def verdict(config: dict, plugin_id: str, keys: list[str]) -> str | None:
    """None when the plugin may run; otherwise why not."""
    plugin = (config.get("plugins") or {}).get(plugin_id)
    if not isinstance(plugin, dict):
        return UNINSTALLED
    if plugin.get("enabled") is False:
        return DISABLED
    states = (config.get("hooks") or {}).get("state") or {}
    for key in keys:
        entry = states.get(key)
        if isinstance(entry, dict) and entry.get("enabled") is False:
            return HOOK_OFF
    for key in keys:
        entry = states.get(key)
        if not (isinstance(entry, dict) and entry.get("trusted_hash")):
            return UNTRUSTED
    return None


class Gate:
    """Re-reads config.toml only when it changes; a failed read blocks (fail closed)."""

    def __init__(self, root: Path = PLUGIN_ROOT, home: Path | None = None, environ=None):
        self.plugin_id = host_plugin_id(root)
        self.keys = hook_keys(root, self.plugin_id) if self.plugin_id else []
        self.config = (home or codex_home(environ)) / "config.toml"
        self._seen: tuple | None = None
        self._reason: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.plugin_id) and tomllib is not None

    def check(self) -> str | None:
        if not self.active:
            return None
        for attempt in range(2):  # Codex may be rewriting the file this very moment
            try:
                st = self.config.stat()
                seen = (st.st_mtime_ns, st.st_size)
                if seen == self._seen:
                    return self._reason
                with open(self.config, "rb") as f:
                    doc = tomllib.load(f)
            except FileNotFoundError:
                self._seen, self._reason = None, UNINSTALLED
                return UNINSTALLED
            except (OSError, ValueError):
                if attempt == 0:
                    time.sleep(0.1)
                    continue
                self._seen = None
                return UNREADABLE
            self._seen, self._reason = seen, verdict(doc, self.plugin_id, self.keys)
            return self._reason
        return UNREADABLE  # pragma: no cover


_default: Gate | None = None


def check() -> str | None:
    """The process-wide gate for this installation (None = allowed)."""
    global _default
    if _default is None:
        _default = Gate()
    return _default.check()
