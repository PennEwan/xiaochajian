"""Start the uploader process when sync needs it and it is not already running.

Called by every recording hook (after the session lock is released) and by the CLI.
Checking costs a lock attempt on sync\\worker.lock; a spawn happens at most once per
30 s, so a crashing uploader cannot turn every hook into a process launch. The child is
detached, breaks away from Codex's job object when allowed and inherits no handles (as the
recharge window does), so it can finish uploading after Codex exits.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from .. import store
from ..paths import Paths
from . import gate as gate_mod
from . import state as sstate
from .config import SyncPaths

LIB_DIR = Path(__file__).resolve().parents[2]
SPAWN_INTERVAL_S = 30.0
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def worker_running(sp: SyncPaths) -> bool:
    return store.is_held(sp.worker_lock)


def wait_until_stopped(sp: SyncPaths, timeout_s: float = 5.0) -> bool:
    """After a pause / revoke the uploader exits within one poll; True once it has."""
    deadline = time.monotonic() + timeout_s
    while worker_running(sp):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def _pythonw() -> str:
    exe = Path(sys.executable)
    windowed = exe.with_name("pythonw.exe")
    return str(windowed) if os.name == "nt" and windowed.exists() else str(exe)


def worker_command(paths: Paths, environ=None) -> tuple[list[str], dict]:
    env = dict(os.environ if environ is None else environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(LIB_DIR), env.get("PYTHONPATH")) if p)
    env["PYTHONIOENCODING"] = "utf-8"
    env["CTR_DATA_DIR"] = str(paths.root)  # the uploader must serve the same data directory
    return [_pythonw(), "-m", "ctr.sync"], env


def spawn(argv: list[str], env: dict, cwd: str) -> None:
    import subprocess  # only paid for when a process is actually started

    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, env=env, cwd=cwd)
    if os.name != "nt":
        subprocess.Popen(argv, start_new_session=True, **kw)
        return
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(argv, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except PermissionError:  # the job does not allow breakaway
        subprocess.Popen(argv, creationflags=base, **kw)


def kick(paths: Paths, st: sstate.SyncState | None = None, force: bool = False, now=time.time) -> bool:
    """Start the uploader if sync wants it and it is not running. True when one was started."""
    sp = SyncPaths(paths)
    st = st if st is not None else sstate.load(sp)
    if not sstate.wants_worker(st) or worker_running(sp):
        return False
    if not st.pending_ops and gate_mod.check():  # neither the plugin nor the service may run it
        return False
    if not force:
        try:
            if now() - sp.spawn_stamp.stat().st_mtime < SPAWN_INTERVAL_S:
                return False
        except OSError:
            pass
    sp.root.mkdir(parents=True, exist_ok=True)
    sp.spawn_stamp.touch()
    argv, env = worker_command(paths)
    # Windows cannot delete a directory that is some process's cwd: not the plugin cache (uninstall
    # and `ctr install` replace it) and not the data directory (`卸载.cmd` may purge it).
    spawn(argv, env, tempfile.gettempdir())
    return True
