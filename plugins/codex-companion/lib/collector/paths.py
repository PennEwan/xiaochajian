"""Collector file locations (%LOCALAPPDATA%\\CodexCompanion)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "CodexCompanion"
TASK_NAME = "CodexCompanion\\Collector"
TASK_NAME_FLAT = "CodexCompanion-Collector"  # /tn accepts "A\B"; /query by folder needs the split


def default_root(environ=None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get("COMPANION_DATA_DIR")
    if override:
        return Path(override)
    local = env.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_DIR_NAME
    try:
        return Path.home() / ".codex-companion"
    except RuntimeError:  # no USERPROFILE (a stripped-down service environment)
        return Path(env.get("TEMP") or env.get("TMP") or ".") / APP_DIR_NAME


@dataclass(frozen=True)
class CollectorPaths:
    root: Path

    @classmethod
    def default(cls, environ=None) -> "CollectorPaths":
        return cls(default_root(environ))

    # --- contract & state --------------------------------------------------------------------
    @property
    def contract(self) -> Path:
        return self.root / "contract.json"

    @property
    def contract_lock(self) -> Path:
        return self.root / "contract.lock"

    @property
    def service_state(self) -> Path:
        return self.root / "service-state.json"

    @property
    def status(self) -> Path:
        return self.root / "collector-status.json"

    @property
    def log(self) -> Path:
        return self.root / "collector.log"

    @property
    def config(self) -> Path:
        return self.root / "collector-config.json"

    @property
    def lock(self) -> Path:
        return self.root / "collector.lock"

    @property
    def token(self) -> Path:
        return self.root / "ipc.token"

    # --- data --------------------------------------------------------------------------------
    @property
    def spool(self) -> Path:
        return self.root / "spool"

    @property
    def db(self) -> Path:
        return self.root / "reader.sqlite3"

    @property
    def device(self) -> Path:
        return self.root / "device.bin"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.spool.mkdir(parents=True, exist_ok=True)
