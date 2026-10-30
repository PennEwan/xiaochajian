"""What was built: product.json in the plugin root (written by packaging/build.py).

Running from the source tree there is no product.json; the defaults below describe a
development build with no backends configured.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
NAME = "CDK 充值"
PRODUCT_ID = "codex-companion"
MARKETPLACE = "codex-companion"
PLUGIN = "codex-companion"


@dataclass(frozen=True)
class Product:
    root: Path
    name: str = NAME
    version: str = "0.0.0-dev"
    plugin: str = PLUGIN
    marketplace: str = MARKETPLACE
    recharge_base_url: str | None = None
    sync_base_url: str | None = None
    update_manifest_url: str | None = None
    has_hooks: bool = False        # the shipped plugin is MCP only; hooks are optional
    min_kernel: str = "0.163.0"
    tested_kernels: tuple = ("0.163.0-alpha.10.2",)
    tested_apps: tuple = ("26.1013.4894.0",)
    extra: dict = field(default_factory=dict)

    @property
    def plugin_id(self) -> str:
        return f"{self.plugin}@{self.marketplace}"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def hooks_file(self) -> Path:
        return self.root / "hooks" / "hooks.json"

    def declares_hooks(self) -> bool:
        """Does the manifest ask Codex to run hooks? Decides whether trust/compat care about them."""
        if self.has_hooks:
            return True
        try:
            manifest = json.loads((self.root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(manifest.get("hooks"), str)


def _str(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def load(root: Path = PLUGIN_ROOT) -> Product:
    try:
        doc = json.loads((root / "product.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    compat = doc.get("compat") if isinstance(doc.get("compat"), dict) else {}
    plugin_id = _str(doc.get("plugin_id")) or f"{PLUGIN}@{MARKETPLACE}"
    plugin, _, marketplace = plugin_id.partition("@")
    return Product(
        root=root,
        name=_str(doc.get("name")) or NAME,
        version=_str(doc.get("version")) or "0.0.0-dev",
        plugin=plugin,
        marketplace=marketplace or MARKETPLACE,
        recharge_base_url=_str(doc.get("recharge_base_url")),
        sync_base_url=_str(doc.get("sync_base_url")),
        update_manifest_url=_str(doc.get("update_manifest_url")),
        has_hooks=doc.get("has_hooks") is True,
        min_kernel=_str(compat.get("min_kernel")) or "0.163.0",
        tested_kernels=tuple(v for v in compat.get("tested_kernels") or () if isinstance(v, str))
        or ("0.163.0-alpha.10.2",),
        tested_apps=tuple(v for v in compat.get("tested_apps") or () if isinstance(v, str)) or ("26.1013.4894.0",),
    )


def version_key(version: str | None) -> tuple:
    """Numeric release part: '0.163.0-alpha.10.2' -> (0, 163, 0); '1.2' -> (1, 2, 0)."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", version or "")
    nums = [int(n) for n in m.group(1).split(".")] if m else []
    return tuple((nums + [0, 0, 0])[:max(3, len(nums))])
