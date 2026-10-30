"""Why does `account/read` fail inside the App? Re-run it with the environment Codex gives MCP servers.

Codex passes MCP servers a fixed whitelist of environment variables (measured in the plugin's lifecycle
log) plus whatever `.mcp.json` lists in `env_vars`. This calls account/read through a fresh App-kernel
`app-server` twice: with that whitelist only, and with the proxy variables added. Prints only outcomes.

    python scripts\\native_account_env_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "codex-task-recorder" / "lib"))
from ctr import kernel  # noqa: E402

WHITELIST = ["APPDATA", "CODEX_CLI_PATH", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH", "PATHEXT",
             "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "SHELL", "SYSTEMDRIVE", "SYSTEMROOT",
             "TEMP", "TMP", "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR"]
PROXY = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]


def account_read(exe: Path, env: dict) -> dict:
    started = time.monotonic()
    p = subprocess.Popen([str(exe), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, cwd=os.environ.get("TEMP"))
    msgs = [{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "env_check", "version": "1"}}},
            {"method": "initialized", "params": {}},
            {"id": 2, "method": "account/read", "params": {"refreshToken": False}}]
    p.stdin.write(b"".join((json.dumps(m) + "\n").encode() for m in msgs))
    p.stdin.flush()
    result = None
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg.get("id") == 2:
            result = msg
            break
    p.stdin.close()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
    err = p.stderr.read().decode("utf-8", "replace")[-300:]
    if result is None:
        return {"outcome": "no_response", "seconds": round(time.monotonic() - started, 1), "stderr_tail": err}
    if "error" in result:
        return {"outcome": "error", "error": result["error"].get("message", "")[:200], "seconds": round(time.monotonic() - started, 1)}
    acct = (result.get("result") or {}).get("account") or {}
    return {"outcome": "ok", "type": acct.get("type"), "plan": acct.get("planType"), "seconds": round(time.monotonic() - started, 1)}


def main() -> int:
    exe, _ = kernel.find_app_kernel()
    base = {k: os.environ[k] for k in WHITELIST if k in os.environ}
    base["CODEX_CLI_PATH"] = str(exe)
    with_proxy = {**base, **{k: os.environ[k] for k in PROXY if k in os.environ}}
    out = {"proxy_vars_present_in_user_env": [k for k in PROXY if k in os.environ],
           "mcp_whitelist_only": account_read(exe, base), "whitelist_plus_proxy": account_read(exe, with_proxy)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
