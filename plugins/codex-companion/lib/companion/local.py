"""The bundle's own data directory (%LOCALAPPDATA%\\CodexCompanion) and its small JSON files.

    state.json    consent to record + upload, update mode, install directory and version
    update.json   last update check and what it found
    backup\\       config.toml copies taken before install / uninstall touched it
    updates\\      downloaded installers (verified before they run)
    companion.log fixed identifiers and numbers only
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ctr import store

APP_DIR = "CodexCompanion"


class Paths:
    def __init__(self, root: Path):
        self.root = Path(root)

    @classmethod
    def default(cls) -> "Paths":
        override = os.environ.get("COMPANION_DATA_DIR")
        if override:
            return cls(Path(override))
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return cls(Path(base) / APP_DIR)

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def update(self) -> Path:
        return self.root / "update.json"

    @property
    def update_lock(self) -> Path:
        return self.root / "update.lock"

    @property
    def update_stamp(self) -> Path:
        return self.root / "update.stamp"

    @property
    def backup(self) -> Path:
        return self.root / "backup"

    @property
    def downloads(self) -> Path:
        return self.root / "updates"

    @property
    def log(self) -> Path:
        return self.root / "companion.log"


def read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_json(path: Path, doc: dict) -> None:
    """Atomic replace; a per-process temp name, since two hooks may write at the same moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        store.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def update_json(path: Path, **changes) -> dict:
    doc = read_json(path)
    doc.update(changes)
    write_json(path, doc)
    return doc
