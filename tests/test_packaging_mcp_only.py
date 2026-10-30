"""The shipped plugin has no hooks: it is one MCP server and nothing else.

These are packaging invariants, not behaviour tests: if someone re-adds `"hooks"` to the manifest
or restores the staging line, the product silently goes back to "trust 12 hooks" onboarding and
per-prompt process launches.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "codex-companion"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
MCP_JSON = PLUGIN / ".mcp.json"


def test_manifest_declares_mcp_and_no_hooks():
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "hooks" not in doc, "the companion plugin must not declare hooks any more"
    assert doc.get("mcpServers") == "./.mcp.json"
    assert doc["version"].startswith("2."), "a manifest change needs a new major version"


def test_mcp_config_points_at_the_bundled_launcher():
    doc = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    server = doc["mcpServers"]["companion_collector"]
    assert server["command"] == "cmd.exe"
    assert any("mcp.cmd" in str(arg) for arg in server["args"])
    assert (PLUGIN / "bin" / "mcp.cmd").is_file()


def test_build_does_not_stage_hooks():
    text = (REPO / "packaging" / "build.py").read_text(encoding="utf-8")
    assert 'copytree(src / "hooks"' not in text, "hooks must not be staged into the package"
    assert 'hooks_text = json.dumps(make_hooks.generate()' not in text


def test_hook_gate_finds_nothing_to_gate():
    """`hostgate` derives its keys from the manifest: no hooks means no per-hook trust."""
    sys.path.insert(0, str(REPO / "plugins" / "codex-task-recorder" / "lib"))
    try:
        from ctr.sync import hostgate
    finally:
        sys.path.pop(0)
    # a source checkout has no product.json, so the id comes from the manifest/path; what matters
    # is that no hook keys are derived from a manifest that declares none
    assert hostgate.hook_keys(PLUGIN, "codex-companion@codex-companion") == []


def test_mcp_server_offers_the_user_facing_tools():
    env = {"PYTHONPATH": str(PLUGIN / "lib"), "PYTHONIOENCODING": "utf-8"}
    # the collector imports ctr / csr lazily, but the MCP surface itself needs only stdlib
    proc = subprocess.run([sys.executable, "-m", "collector", "mcp"],
                          input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, **env}, timeout=60)
    line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    names = {tool["name"] for tool in json.loads(line)["result"]["tools"]}
    assert {"collector_status", "collector_pause", "collector_resume", "collector_stop",
            "collector_open_panel", "recharge_open"} == names, "recharge_open replaces the /充值 hook"


def test_initialize_reports_the_server_name():
    env = {"PYTHONPATH": str(PLUGIN / "lib"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "collector", "mcp"],
                          input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {}}),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, **env}, timeout=60)
    doc = json.loads((proc.stdout or "{}").strip().splitlines()[-1])
    assert doc["result"]["serverInfo"]["name"] == "codex-companion-collector"
    assert doc["result"]["protocolVersion"] == "2024-11-05"
