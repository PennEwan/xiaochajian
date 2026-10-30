"""Find the codex.exe the Codex desktop App actually runs.

The App ships its own kernel (e.g. 0.163.0-alpha.10.2) under
%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe, which can differ from the
npm `codex` on PATH. Hook trust hashes can differ between versions, so every
install/test command must use the App's kernel.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_VERSION = re.compile(r"codex-cli\s+(\S+)")
# Callers include windowless processes (the control panel, the update checker): without this
# every console child — powershell, codex.exe — would flash a console window on screen.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _bin_root() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "OpenAI" / "Codex" / "bin" if local else None


def version_of(exe: Path) -> str | None:
    try:
        proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION.search(proc.stdout + proc.stderr)
    return m.group(1) if m else None


def _version_key(version: str | None) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


def _running_kernels() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Process codex -ErrorAction SilentlyContinue | ForEach-Object { $_.Path }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    return [Path(line.strip()) for line in proc.stdout.splitlines() if line.strip()]


def find_app_kernel() -> tuple[Path, str | None] | None:
    override = os.environ.get("CTR_CODEX_BIN")
    if override:
        return Path(override), version_of(Path(override))
    root = _bin_root()
    if root is None or not root.exists():
        return None
    root_norm = os.path.normcase(str(root))
    for exe in _running_kernels():
        if os.path.normcase(str(exe)).startswith(root_norm) and exe.exists():
            return exe, version_of(exe)
    candidates = [(version_of(p), p) for p in root.glob("*/codex.exe")]
    candidates = [(v, p) for v, p in candidates if v]
    if not candidates:
        return None
    version, exe = max(candidates, key=lambda c: (_version_key(c[0]), c[1].stat().st_mtime))
    return exe, version


def run(exe: Path, args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW)
