"""Generate plugins/codex-companion/hooks/hooks.json from the recorder's hook set.

Same events, async flags and timeouts as the recorder (UserPromptSubmit, Interrupt and
SessionEnd stay synchronous); every command runs the plugin's own Python on hooks/hook.py.
The trust hash covers this file's text (not the plugin version), so regenerate it only when
the hook set really changes — users then have to trust the hooks again.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "plugins" / "codex-task-recorder" / "hooks" / "hooks.json"
TARGET = REPO / "plugins" / "codex-companion" / "hooks" / "hooks.json"
COMMAND = 'python3 "${PLUGIN_ROOT}/hooks/hook.py"'
COMMAND_WINDOWS = '& "${PLUGIN_ROOT}\\runtime\\python.exe" "${PLUGIN_ROOT}\\hooks\\hook.py"'


def generate() -> dict:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))["hooks"]
    out = {}
    for event, groups in source.items():
        hook = groups[0]["hooks"][0]
        entry = {"type": "command", "command": COMMAND, "commandWindows": COMMAND_WINDOWS,
                 "timeout": hook["timeout"]}
        if hook.get("async"):
            entry["async"] = True
        out[event] = [{"hooks": [entry]}]
    return {"hooks": out}


if __name__ == "__main__":
    text = json.dumps(generate(), indent=2, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        sys.exit(0 if TARGET.read_text(encoding="utf-8") == text else 1)
    TARGET.write_text(text, encoding="utf-8")
    print(TARGET)
