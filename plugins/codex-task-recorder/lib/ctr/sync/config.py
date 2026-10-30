"""Sync file locations and backend settings (%LOCALAPPDATA%\\CodexTaskRecorder\\sync)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..paths import Paths

# No default backend, as for the recharge plugin: an unset address must not silently mean
# some local port. Development: `ctr sync config --base-url http://127.0.0.1:18788`.
DEFAULT_TIMEOUT = 15.0


class SyncPaths:
    def __init__(self, paths: Paths):
        self.data = paths
        self.root = paths.root / "sync"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def state_lock(self) -> Path:
        return self.root / "state.lock"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def device(self) -> Path:
        return self.root / "device.bin"

    @property
    def cursors(self) -> Path:
        return self.root / "cursors"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    @property
    def worker_lock(self) -> Path:
        return self.root / "worker.lock"

    @property
    def spawn_stamp(self) -> Path:
        return self.root / "spawn.stamp"

    @property
    def log(self) -> Path:
        return self.root / "sync.log"


@dataclass
class Config:
    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT


def load(sp: SyncPaths) -> Config:
    cfg = Config()
    try:
        raw = json.loads(sp.config.read_text(encoding="utf-8-sig"))  # tolerate a BOM (PowerShell 5)
    except (OSError, ValueError):
        return cfg
    if isinstance(raw, dict):
        if isinstance(raw.get("base_url"), str) and raw["base_url"].strip():
            cfg.base_url = raw["base_url"].strip()
        if isinstance(raw.get("timeout"), (int, float)) and 1 <= raw["timeout"] <= 120:
            cfg.timeout = float(raw["timeout"])
    return cfg


def save(sp: SyncPaths, cfg: Config) -> None:
    sp.root.mkdir(parents=True, exist_ok=True)
    sp.config.write_text(json.dumps({"base_url": cfg.base_url, "timeout": cfg.timeout}, indent=2),
                         encoding="utf-8")
