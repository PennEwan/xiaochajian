"""Fixtures for the Codex 助手包 tests: an installed-looking bundle in a temp directory, a
throwaway CODEX_HOME, the fake kernel (fake_codex.py) and private data directories."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from companion import local, product, setup
from ctr.paths import Paths as RecorderPaths

REPO = Path(__file__).resolve().parents[1]
COMPANION = REPO / "plugins" / "codex-companion"
FAKE = Path(__file__).resolve().parent / "fake_codex.py"
PID = "codex-companion@codex-companion"


def stage(install_dir: Path, **product_fields) -> Path:
    """<install>\\marketplace\\plugins\\codex-companion as the installer lays it out (no runtime)."""
    market = install_dir / "marketplace"
    root = market / "plugins" / "codex-companion"
    for sub in (".codex-plugin", "hooks"):
        shutil.copytree(COMPANION / sub, root / sub)
    doc = {"name": "Codex 助手包", "version": "1.0.0", "plugin_id": PID}
    doc.update(product_fields)
    (root / "product.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (market / ".agents" / "plugins").mkdir(parents=True)
    (market / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps({
        "name": "codex-companion", "interface": {"displayName": "Codex 助手包"},
        "plugins": [{"name": "codex-companion", "source": {"source": "local", "path": "./plugins/codex-companion"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Productivity"}]}), encoding="utf-8")
    return root


def legacy_market(base: Path) -> Path:
    """A ctr-local marketplace like the repo's (two development plugins)."""
    market = base / "legacy"
    for name in ("codex-task-recorder", "codex-cdk-recharge"):
        src = REPO / "plugins" / name
        for sub in (".codex-plugin", "hooks"):
            shutil.copytree(src / sub, market / "plugins" / name / sub)
    (market / ".agents" / "plugins").mkdir(parents=True)
    shutil.copy(REPO / ".agents" / "plugins" / "marketplace.json", market / ".agents" / "plugins" / "marketplace.json")
    return market


class CompanionCase(unittest.TestCase):
    product_fields: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="companion-test-"))
        self.home = self.tmp / "codex-home"
        self.home.mkdir()
        self.install_dir = self.tmp / "Programs" / "CodexCompanion"
        self.root = stage(self.install_dir, **self.product_fields)
        self.product = product.load(self.root)
        self.fake = self.tmp / "codex.cmd"
        self.fake.write_text(f'@"{sys.executable}" "{FAKE}" %*\n', encoding="ascii")
        self.kernel_log = self.tmp / "kernel.log"
        env = {"CODEX_HOME": str(self.home), "CTR_CODEX_BIN": str(self.fake),
               "FAKE_CODEX_LOG": str(self.kernel_log), "COMPANION_DATA_DIR": str(self.tmp / "companion"),
               "CTR_DATA_DIR": str(self.tmp / "recorder"), "CDKR_HOME": str(self.tmp / "cdkr"),
               "COMPANION_APP_VERSION": "26.1013.4894.0"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        spawn = mock.patch("ctr.sync.launcher.spawn")  # never start real uploader processes
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)
        self.cpaths = local.Paths(self.tmp / "companion")
        self.rpaths = RecorderPaths(self.tmp / "recorder")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self) -> setup.Context:
        return setup.Context(product=self.product, home=self.home, cpaths=self.cpaths, rpaths=self.rpaths,
                             cdkr_root=self.tmp / "cdkr", install_dir=self.install_dir)

    def config_text(self) -> str:
        cfg = self.home / "config.toml"
        return cfg.read_text(encoding="utf-8") if cfg.exists() else ""

    def kernel_calls(self) -> list[str]:
        return self.kernel_log.read_text(encoding="utf-8").splitlines() if self.kernel_log.exists() else []

    def trust_all(self, plugin_id: str = PID) -> None:
        """What the user's 「全部信任」 click writes (in a throwaway CODEX_HOME only)."""
        from ctr.sync import hostgate

        keys = hostgate.hook_keys(self.root, plugin_id)
        with open(self.home / "config.toml", "a", encoding="utf-8") as f:
            for key in keys:
                f.write(f'\n[hooks.state."{key}"]\ntrusted_hash = "sha256:fake"\n')
