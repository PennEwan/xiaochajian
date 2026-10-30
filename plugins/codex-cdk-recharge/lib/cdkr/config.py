"""Data directory and backend settings (%LOCALAPPDATA%\\CodexCdkRecharge)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# No default backend: an unset address must not silently mean "some local port" that any
# process (or another user on a shared server) could listen on. Development points it at
# scripts/mock_recharge_server.py with `cdkr.cmd config --base-url http://127.0.0.1:18787`.
DEFAULT_TIMEOUT = 15.0


class Paths:
    def __init__(self, root: Path):
        self.root = Path(root)

    @classmethod
    def default(cls) -> "Paths":
        override = os.environ.get("CDKR_HOME")
        if override:
            return cls(Path(override))
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return cls(Path(base) / "CodexCdkRecharge")

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def log(self) -> Path:
        return self.root / "cdkr.log"


@dataclass
class Config:
    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT


def load(paths: Paths) -> Config:
    cfg = Config()
    try:
        raw = json.loads(paths.config.read_text(encoding="utf-8-sig"))  # tolerate a BOM (PowerShell 5)
    except (OSError, ValueError):
        return cfg
    if isinstance(raw, dict):
        if isinstance(raw.get("base_url"), str) and raw["base_url"].strip():
            cfg.base_url = raw["base_url"].strip()
        if isinstance(raw.get("timeout"), (int, float)) and 1 <= raw["timeout"] <= 120:
            cfg.timeout = float(raw["timeout"])
    return cfg


def save(paths: Paths, cfg: Config) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(json.dumps({"base_url": cfg.base_url, "timeout": cfg.timeout}, indent=2),
                            encoding="utf-8")
