"""Stand-in for the Codex App kernel (codex.exe) in the companion setup tests.

Behaves like kernel 0.163.0-alpha.10.2 as measured (docs/superpowers/specs/…p4…, section 2):
  plugin marketplace add <dir> | remove <name> | list
  plugin add <plugin>@<market>   copy into $CODEX_HOME/plugins/cache/<market>/<plugin>/<version>;
                                 like the real kernel it turns a disabled plugin back on
  plugin remove <id>             drops [plugins."<id>"] and the cache — but NOT hooks.state
  app-server                     initialize, hooks/list, account/read,
                                 config/value/write (plugins.<id>.enabled only) — JSON-RPC over stdio
  --version                      codex-cli $FAKE_CODEX_VERSION

Knobs: FAKE_CODEX_VERSION, FAKE_CODEX_NO_PLUGIN=1 (no `plugin` command), FAKE_CODEX_NO_HOOKS=1
(hooks/list returns nothing), FAKE_CODEX_LOG=<file> (one line per invocation).
Trust: a hooks.state entry whose trusted_hash equals TRUSTED_HASH counts as trusted.
"""
import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

TRUSTED_HASH = "sha256:fake"
HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
CONFIG = HOME / "config.toml"


def text() -> str:
    return CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""


def doc() -> dict:
    return tomllib.loads(text())


def save(t: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(t, encoding="utf-8")


def drop_table(t: str, header: str) -> str:
    out, skip = [], False
    for line in t.splitlines(keepends=True):
        if line.startswith("["):
            skip = line.strip() == header
        if not skip:
            out.append(line)
    return "".join(out)


def add_table(t: str, header: str, body: str) -> str:
    if t and not t.endswith("\n"):
        t += "\n"
    return t + ("\n" if t else "") + header + "\n" + body + "\n"


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def set_enabled(plugin_id: str, enabled: bool) -> None:
    header = f'[plugins."{plugin_id}"]'
    save(add_table(drop_table(text(), header), header, f"enabled = {'true' if enabled else 'false'}"))


def market_root(name: str) -> Path | None:
    entry = (doc().get("marketplaces") or {}).get(name)
    if not entry:
        return None
    src = entry["source"]
    return Path(src[4:] if src.startswith("\\\\?\\") else src)


def plugin_cmd(args: list[str]) -> int:
    if os.environ.get("FAKE_CODEX_NO_PLUGIN") == "1":
        print("error: unrecognized subcommand 'plugin'", file=sys.stderr)
        return 2
    if args[:1] == ["--help"]:
        print("Commands:\n  add\n  list\n  marketplace\n  remove")
        return 0
    if args[:2] == ["marketplace", "add"]:
        root = Path(args[2])
        name = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))["name"]
        t = drop_table(text(), f"[marketplaces.{name}]")
        save(add_table(t, f"[marketplaces.{name}]", f"source_type = \"local\"\nsource = '\\\\?\\{root}'"))
        print(f"Added marketplace `{name}`")
        return 0
    if args[:2] == ["marketplace", "remove"]:
        if not market_root(args[2]):
            print(f"marketplace `{args[2]}` not found", file=sys.stderr)
            return 1
        save(drop_table(text(), f"[marketplaces.{args[2]}]"))
        return 0
    if args[:2] == ["marketplace", "list"]:
        for name, entry in (doc().get("marketplaces") or {}).items():
            print(name, entry.get("source"))
        return 0
    if args[:1] == ["add"]:
        plugin, market = args[1].split("@")
        root = market_root(market)
        if root is None:
            print("unknown marketplace", file=sys.stderr)
            return 1
        listing = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
        rel = next(p["source"]["path"] for p in listing["plugins"] if p["name"] == plugin)
        src = (root / rel).resolve()
        version = json.loads((src / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        base = HOME / "plugins" / "cache" / market / plugin
        shutil.rmtree(base, ignore_errors=True)
        shutil.copytree(src, base / version)
        set_enabled(args[1], True)  # measured: `plugin add` re-enables a disabled plugin
        print(f"Added plugin `{plugin}`")
        return 0
    if args[:1] == ["remove"]:
        plugin, market = args[1].split("@")
        if args[1] not in (doc().get("plugins") or {}):
            print("not installed", file=sys.stderr)
            return 1
        save(drop_table(text(), f'[plugins."{args[1]}"]'))
        shutil.rmtree(HOME / "plugins" / "cache" / market / plugin, ignore_errors=True)
        return 0
    if args[:1] == ["list"]:
        for pid, entry in (doc().get("plugins") or {}).items():
            print(f"{pid}  installed, {'enabled' if entry.get('enabled', True) else 'disabled'}")
        return 0
    return 2


def hooks_list() -> list[dict]:
    if os.environ.get("FAKE_CODEX_NO_HOOKS") == "1":
        return []
    d = doc()
    states = (d.get("hooks") or {}).get("state") or {}
    out = []
    for pid, entry in (d.get("plugins") or {}).items():
        if entry.get("enabled") is False:
            continue
        plugin, market = pid.split("@")
        base = HOME / "plugins" / "cache" / market / plugin
        versions = sorted(base.iterdir()) if base.exists() else []
        if not versions:
            continue
        # Codex runs only the hooks a plugin's manifest declares: a plugin without a "hooks"
        # entry has none, and `hooks/list` must say so (the shipped companion is that case).
        manifest = versions[-1] / ".codex-plugin" / "plugin.json"
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8")).get("hooks")
        except (OSError, ValueError):
            declared = "./hooks/hooks.json"
        if not isinstance(declared, str):
            continue
        hooks_file = versions[-1] / declared.replace("\\", "/").removeprefix("./")
        try:
            spec = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]
        except (OSError, ValueError, KeyError):
            continue
        for event, groups in spec.items():
            for gi, group in enumerate(groups):
                for hi, _ in enumerate(group["hooks"]):
                    key = f"{pid}:{declared.removeprefix('./')}:{snake(event)}:{gi}:{hi}"
                    st = states.get(key) or {}
                    trust = ("trusted" if st.get("trusted_hash") == TRUSTED_HASH else
                             "modified" if st.get("trusted_hash") else "untrusted")
                    out.append({"key": key, "pluginId": pid, "eventName": event, "trustStatus": trust,
                                "enabled": st.get("enabled") is not False, "currentHash": TRUSTED_HASH})
    return out


def app_server() -> int:
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method = msg["method"]
        if method == "initialize":
            result = {"userAgent": "fake"}
        elif method == "hooks/list":
            result = {"data": [{"cwd": msg["params"]["cwds"][0], "hooks": hooks_list(), "warnings": [], "errors": []}]}
        elif method == "config/value/write":
            key = msg["params"]["keyPath"]
            if not (key.startswith("plugins.") and key.endswith(".enabled")):
                print(json.dumps({"id": msg["id"], "error": {"code": -32602, "message": "unsupported"}}), flush=True)
                continue
            set_enabled(key[len("plugins."):-len(".enabled")], bool(msg["params"]["value"]))
            result = {"status": "ok", "version": "fake", "filePath": str(CONFIG)}
        elif method == "account/read":
            result = {"account": {"type": "chatgpt", "email": "user@example.com", "planType": "plus"},
                      "workspaceRouting": {"chatgptAccountId": "acct-1"}}
        else:
            print(json.dumps({"id": msg["id"], "error": {"code": -32601, "message": "unknown"}}), flush=True)
            continue
        print(json.dumps({"id": msg["id"], "result": result}), flush=True)
    return 0


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_CODEX_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(" ".join(argv) + "\n")
    if argv[:1] == ["--version"]:
        print(f"codex-cli {os.environ.get('FAKE_CODEX_VERSION', '0.163.0-alpha.10.2')}")
        return 0
    if argv[:1] == ["plugin"]:
        return plugin_cmd(argv[1:])
    if argv[:1] == ["app-server"]:
        return app_server()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
