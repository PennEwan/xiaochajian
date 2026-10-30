"""Start the bundle's own windows and helpers as detached processes.

Same rules as the recharge window and the uploader: detached, out of Codex's job object when
allowed, no inherited handles (an inherited pipe would keep Codex waiting on the hook), and the
system temp directory as cwd (Windows cannot delete a directory some process uses as cwd, and
uninstall / update replace the plugin directories).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1]
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def pythonw() -> str:
    exe = Path(sys.executable)
    windowed = exe.with_name("pythonw.exe")
    return str(windowed) if os.name == "nt" and windowed.exists() else str(exe)


def child_env(environ=None) -> dict:
    """The bundled runtime finds lib\\ through its ._pth file; a source checkout needs PYTHONPATH."""
    env = dict(os.environ if environ is None else environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(LIB_DIR), env.get("PYTHONPATH")) if p)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def spawn(argv: list[str], cwd: str | None = None, env: dict | None = None) -> subprocess.Popen:
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
              env=env if env is not None else child_env(), cwd=cwd or tempfile.gettempdir())
    if os.name != "nt":
        return subprocess.Popen(argv, start_new_session=True, **kw)
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except PermissionError:  # the job does not allow breakaway
        return subprocess.Popen(argv, creationflags=base, **kw)


def spawn_module(module: str, args: list[str]) -> subprocess.Popen:
    return spawn([pythonw(), "-m", module, *args])


def open_panel() -> bool:
    spawn_module("companion", ["panel"])
    return True
