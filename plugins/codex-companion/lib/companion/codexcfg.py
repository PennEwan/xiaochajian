"""Codex's config.toml: what it says about this plugin, and removing exactly our entries.

Install and uninstall change config.toml only through Codex's own commands (`codex plugin …`),
with one exception: `codex plugin remove` / `marketplace remove` leave the hook trust entries
`[hooks.state."<plugin id>:…"]` behind (measured on kernel 0.163.0-alpha.10.2). Uninstall
removes those tables — and any of our plugin / marketplace tables still present — line by
line, then proves the edit: the file must parse, and parse to exactly the old document minus
our keys. Anything else and the original text is put back.
"""
from __future__ import annotations

import copy
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tomllib

from ctr.sync import hostgate
from ctr.store import replace

_KEY = r'(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|\'[^\']*\')'
_HEADER = re.compile(rf"^\[(\[)?\s*({_KEY}(?:\s*\.\s*{_KEY})*)\s*\](\])?\s*(?:#.*)?$")


def codex_home(environ=None) -> Path:
    return hostgate.codex_home(environ)


def config_path(home: Path) -> Path:
    return home / "config.toml"


def read(home: Path) -> dict | None:
    """The parsed config; {} when there is none; None when it cannot be read or parsed."""
    try:
        with open(config_path(home), "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None


@dataclass
class PluginState:
    installed: bool = False
    enabled: bool = False
    hooks_total: int = 0
    hooks_trusted: int = 0
    hooks_off: int = 0
    marketplace_root: str | None = None
    reason: str | None = None          # hostgate verdict: why recording/upload may not run
    readable: bool = True


def plugin_state(home: Path, plugin_root: Path, plugin_id: str, marketplace: str) -> PluginState:
    doc = read(home)
    if doc is None:
        return PluginState(readable=False, reason=hostgate.UNREADABLE)
    plugin = (doc.get("plugins") or {}).get(plugin_id)
    states = (doc.get("hooks") or {}).get("state") or {}
    keys = hostgate.hook_keys(plugin_root, plugin_id)
    market = (doc.get("marketplaces") or {}).get(marketplace)
    return PluginState(
        installed=isinstance(plugin, dict),
        enabled=isinstance(plugin, dict) and plugin.get("enabled") is not False,
        hooks_total=len(keys),
        hooks_trusted=sum(1 for k in keys if isinstance(states.get(k), dict) and states[k].get("trusted_hash")),
        hooks_off=sum(1 for k in keys if isinstance(states.get(k), dict) and states[k].get("enabled") is False),
        marketplace_root=_strip_verbatim(market.get("source")) if isinstance(market, dict) else None,
        reason=hostgate.verdict(doc, plugin_id, keys),
    )


def _strip_verbatim(path) -> str | None:
    return path[4:] if isinstance(path, str) and path.startswith("\\\\?\\") else path


def installed_plugins(home: Path, marketplace: str) -> list[str]:
    """Plugin ids from one marketplace that config.toml lists."""
    doc = read(home) or {}
    return sorted(pid for pid in (doc.get("plugins") or {}) if pid.endswith("@" + marketplace))


def _parse_header(line: str) -> list[str] | None:
    """Key path of a table header line (`[a."b c".d]` -> ['a', 'b c', 'd']), parsed by tomllib
    itself so quoting and escapes follow TOML exactly; None when the line is not a header."""
    if not _HEADER.match(line):
        return None
    try:
        node = tomllib.loads(line)
    except ValueError:
        return None
    path = []
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
        if isinstance(node, list):  # [[array of tables]]
            node = node[-1] if node else {}
    return path


def is_ours(path: list[str], plugin_id: str, marketplace: str | None) -> bool:
    if len(path) >= 2 and path[0] == "plugins" and path[1] == plugin_id:
        return True
    if marketplace and len(path) >= 2 and path[0] == "marketplaces" and path[1] == marketplace:
        return True
    return len(path) >= 3 and path[:2] == ["hooks", "state"] and path[2].startswith(plugin_id + ":")


def _blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def strip_tables(text: str, plugin_id: str, marketplace: str | None) -> tuple[str, list[str]]:
    """Drop every table that belongs to this plugin; returns (new text, removed headers).

    Everything else stays byte for byte, including comments that sit right above the next table
    (they belong to that table) and blank lines inside multi-line strings."""
    out, removed, skipping, pending = [], [], False, []
    for line in text.splitlines(keepends=True):
        path = _parse_header(line.rstrip("\r\n")) if line.startswith("[") else None
        if path is not None:
            ours = is_ours(path, plugin_id, marketplace)
            if skipping and not ours:
                # Comments / blank lines between our table and the next one belong to the next one;
                # keep a single blank separator.
                while pending and not pending[0].strip() and (not out or not out[-1].strip()):
                    pending.pop(0)
                out.extend(pending)
            pending = []
            skipping = ours
            if ours:
                removed.append(line.strip())
                continue
        if skipping:
            pending = pending + [line] if _blank_or_comment(line) else []
            continue
        out.append(line)
    return "".join(out), removed


def without_ours(doc: dict, plugin_id: str, marketplace: str | None) -> dict:
    doc = copy.deepcopy(doc)
    plugins = doc.get("plugins")
    if isinstance(plugins, dict):
        plugins.pop(plugin_id, None)
    markets = doc.get("marketplaces")
    if marketplace and isinstance(markets, dict):
        markets.pop(marketplace, None)
    states = (doc.get("hooks") or {}).get("state")
    if isinstance(states, dict):
        for key in [k for k in states if k.startswith(plugin_id + ":")]:
            states.pop(key)
    return doc


def _pruned(value):
    """Drop empty tables: `[hooks.state]` written out or implied parse differently but mean the same."""
    if isinstance(value, dict):
        out = {k: _pruned(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if not (isinstance(v, dict) and not v)}
    if isinstance(value, list):
        return [_pruned(v) for v in value]
    return value


def leftovers(doc: dict, plugin_id: str, marketplace: str | None) -> list[str]:
    """Our keys still present in a parsed config."""
    found = []
    if plugin_id in (doc.get("plugins") or {}):
        found.append(f"plugins.{plugin_id}")
    if marketplace and marketplace in (doc.get("marketplaces") or {}):
        found.append(f"marketplaces.{marketplace}")
    found += [f"hooks.state.{k}" for k in ((doc.get("hooks") or {}).get("state") or {})
              if k.startswith(plugin_id + ":")]
    return found


@dataclass
class CleanResult:
    removed: list[str] = field(default_factory=list)
    verified: bool = False
    backup: str | None = None
    error: str | None = None


def backup(home: Path, backup_dir: Path, tag: str) -> Path | None:
    src = config_path(home)
    if not src.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"config.toml.{tag}-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(src, dst)
    return dst


def clean(home: Path, plugin_id: str, marketplace: str | None, backup_dir: Path) -> CleanResult:
    """Remove this plugin's tables (marketplace=None: leave marketplace tables to Codex's command).
    Never raises: the result says what happened."""
    cfg = config_path(home)
    result = CleanResult()
    try:
        text = cfg.read_bytes().decode("utf-8-sig")
        before = tomllib.loads(text)
    except FileNotFoundError:
        result.verified = True
        return result
    except UnicodeDecodeError:
        result.error = "unparsable_before"
        return result
    except ValueError:
        result.error = "unparsable_before"
        return result
    except OSError as exc:
        result.error = type(exc).__name__
        return result
    new_text, result.removed = strip_tables(text, plugin_id, marketplace)
    if not result.removed:
        result.verified = not leftovers(before, plugin_id, marketplace)
        if not result.verified:
            result.error = "not_table_form"
        return result
    tmp = cfg.with_name("config.toml.companion-tmp")
    try:
        saved = backup(home, backup_dir, "uninstall")
        result.backup = str(saved) if saved else None
        try:
            after = tomllib.loads(new_text)
        except ValueError:
            after = None
        if after is None or _pruned(after) != _pruned(without_ours(before, plugin_id, marketplace)):
            result.error = "verify_failed"  # the original stays untouched
            return result
        tmp.write_bytes(new_text.encode("utf-8"))
        replace(tmp, cfg)
        result.verified = True
    except OSError as exc:  # e.g. Codex holds the file longer than replace() waits
        result.error = type(exc).__name__
    finally:
        tmp.unlink(missing_ok=True)
    return result
