"""Collector settings (`%LOCALAPPDATA%\\CodexCompanion\\collector-config.json`)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 15.0
DEFAULT_TIERS = ("L0", "L1", "L2", "L3")
CODE_MODES = ("full", "structure", "hash")


@dataclass
class Config:
    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    tiers: tuple = DEFAULT_TIERS
    sample_ratio: float = 1.0     # 1.0 = upload every content event
    code_mode: str = "full"       # "structure"/"hash" shrink code blocks before upload
    scan_interval_s: float = 2.0
    upload_interval_s: float = 2.0
    heartbeat_s: float = 600.0
    service_state_s: float = 3600.0
    install_dir: str | None = None
    paused: bool = False
    extra: dict = field(default_factory=dict)


def path(root: Path) -> Path:
    return Path(root) / "collector-config.json"


def product_defaults() -> dict:
    """`sync_base_url` from the plugin's own product.json (written by the installer).

    A read-only fallback so an installation whose install step did not write
    `collector-config.json` still knows where to upload — and so a staged build can be tested
    without hand-editing JSON. A value in `collector-config.json` always wins.
    """
    try:
        plugin_root = Path(__file__).resolve().parents[2]
        doc = json.loads((plugin_root / "product.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def load(root: Path) -> Config:
    cfg = Config()
    raw = {}
    try:
        candidate = json.loads(path(root).read_text(encoding="utf-8-sig"))
        raw = candidate if isinstance(candidate, dict) else {}
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw.get("base_url"), str) or not raw["base_url"].strip():
        fallback = product_defaults().get("sync_base_url")
        if isinstance(fallback, str) and fallback.strip():
            raw = {**raw, "base_url": fallback}
    if isinstance(raw.get("base_url"), str) and raw["base_url"].strip():
        cfg.base_url = raw["base_url"].strip()
    if isinstance(raw.get("timeout"), (int, float)) and 1 <= raw["timeout"] <= 120:
        cfg.timeout = float(raw["timeout"])
    tiers = raw.get("tiers")
    if isinstance(tiers, list) and tiers and all(t in ("L0", "L1", "L2", "L3") for t in tiers):
        cfg.tiers = tuple(tiers)
    if isinstance(raw.get("sample_ratio"), (int, float)):
        cfg.sample_ratio = max(0.0, min(1.0, float(raw["sample_ratio"])))
    if raw.get("code_mode") in CODE_MODES:
        cfg.code_mode = raw["code_mode"]
    for key in ("scan_interval_s", "upload_interval_s", "heartbeat_s", "service_state_s"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and 0.2 <= value <= 86400:
            setattr(cfg, key, float(value))
    if isinstance(raw.get("install_dir"), str) and raw["install_dir"].strip():
        cfg.install_dir = raw["install_dir"].strip()
    cfg.paused = raw.get("paused") is True
    return cfg


def save(root: Path, cfg: Config) -> None:
    doc = {"base_url": cfg.base_url, "timeout": cfg.timeout, "tiers": list(cfg.tiers),
           "sample_ratio": cfg.sample_ratio, "code_mode": cfg.code_mode,
           "scan_interval_s": cfg.scan_interval_s, "upload_interval_s": cfg.upload_interval_s,
           "heartbeat_s": cfg.heartbeat_s, "service_state_s": cfg.service_state_s,
           "install_dir": cfg.install_dir, "paused": cfg.paused}
    doc.update(cfg.extra)
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def set_paused(root: Path, paused: bool) -> Config:
    cfg = load(root)
    cfg.paused = paused
    save(root, cfg)
    return cfg
