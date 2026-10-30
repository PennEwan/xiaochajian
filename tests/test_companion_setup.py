"""Install / uninstall of Codex 助手包 against the fake kernel: registration through Codex's own
commands, compatibility verdicts, development plugins, and a config.toml restored on uninstall."""
import json
import os
import tomllib
import unittest
from unittest import mock

from companion import compat, setup
from cdkr import config as cconfig
from ctr.sync import config as sconfig
from ctr.sync import state as sstate
from ctr.sync.config import SyncPaths

from companion_helpers import PID, CompanionCase, legacy_market

def plugin_version() -> str:
    """The version the installer records, read from the manifest instead of hardcoded."""
    import pathlib
    manifest = pathlib.Path(__file__).parents[1] / "plugins" / "codex-companion" / ".codex-plugin" / "plugin.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]



def shipped_plugin_has_hooks() -> bool:
    """The shipped companion is MCP-only; that gate no longer applies to it."""
    import json
    from pathlib import Path

    manifest = Path(__file__).parents[1] / "plugins" / "codex-companion" / ".codex-plugin" / "plugin.json"
    return isinstance(json.loads(manifest.read_text(encoding="utf-8")).get("hooks"), str)


ORIGINAL = '''model = "gpt-6-luna"
notify = [
  "C:\\\\x\\\\notify.exe",
  "turn-ended",
]

[features]
goals = true

[projects.'d:\\codexrouter']
trust_level = "trusted"

[plugins."browser@openai-bundled"]
enabled = true
'''


class InstallTest(CompanionCase):
    product_fields = {"recharge_base_url": "http://127.0.0.1:18787", "sync_base_url": "http://127.0.0.1:18788"}

    def setUp(self):
        super().setUp()
        (self.home / "config.toml").write_text(ORIGINAL, encoding="utf-8")

    def test_fresh_install_registers_marketplace_and_plugin(self):
        result = setup.install(self.ctx())
        self.assertEqual(result["status"], "ok", result)
        doc = tomllib.loads(self.config_text())
        self.assertIn("codex-companion", doc["marketplaces"])
        self.assertTrue(doc["plugins"][PID]["enabled"])
        self.assertTrue((self.home / "plugins/cache/codex-companion/codex-companion" / plugin_version() / "hooks/hook.py").exists())
        self.assertEqual(result["hooks"]["total"], 0)  # MCP only: no hooks to trust
        cached = (self.home / "plugins/cache/codex-companion/codex-companion"
                  / plugin_version() / ".codex-plugin/plugin.json")
        self.assertNotIn("hooks", json.loads(cached.read_text(encoding="utf-8")))
        self.assertNotIn("hooks", doc)  # install never writes trust: the user grants it in the App
        self.assertTrue(result["backup"] and os.path.exists(result["backup"]))

    def test_backends_are_configured_for_both_modules(self):
        setup.install(self.ctx())
        self.assertEqual(cconfig.load(cconfig.Paths(self.tmp / "cdkr")).base_url, "http://127.0.0.1:18787")
        self.assertEqual(sconfig.load(SyncPaths(self.rpaths)).base_url, "http://127.0.0.1:18788")
        state = json.loads(self.cpaths.state.read_text(encoding="utf-8"))
        self.assertEqual(state["installed_version"], self.product.version)
        self.assertEqual(state["install_dir"], str(self.install_dir))

    def test_reinstall_is_idempotent(self):
        setup.install(self.ctx())
        self.trust_all()
        result = setup.install(self.ctx())
        # no hooks are shipped, so trust is not something a reinstall has to preserve
        self.assertEqual(result["hooks"]["total"], 0)
        self.assertEqual(self.config_text().count("[marketplaces.codex-companion]"), 1)
        self.assertEqual(sum("marketplace add" in c for c in self.kernel_calls()), 1)

    def test_marketplace_left_elsewhere_by_an_old_install_is_replaced(self):
        with open(self.home / "config.toml", "a", encoding="utf-8") as f:
            f.write("\n[marketplaces.codex-companion]\nsource_type = \"local\"\nsource = 'D:\\old\\place'\n")
        setup.install(self.ctx())
        root = self.ctx().state().marketplace_root
        self.assertEqual(os.path.normcase(root), os.path.normcase(str(self.install_dir / "marketplace")))

    def test_development_plugins_are_reported_and_removed_on_request(self):
        legacy = legacy_market(self.tmp)
        from fake_codex import main as fake  # noqa: F401 - same behaviour, run through the real CLI below
        from ctr import kernel

        exe = kernel.find_app_kernel()[0]
        kernel.run(exe, ["plugin", "marketplace", "add", str(legacy)])
        for pid in setup.LEGACY_PLUGINS:
            kernel.run(exe, ["plugin", "add", pid])
        with open(self.home / "config.toml", "a", encoding="utf-8") as f:  # trusted dev hooks
            f.write('\n[hooks.state."codex-task-recorder@ctr-local:hooks/hooks.json:stop:0:0"]\n'
                    'trusted_hash = "sha256:fake"\n')
        result = setup.install(self.ctx())
        self.assertEqual(sorted(result["legacy"]), sorted(setup.LEGACY_PLUGINS))
        removed = setup.remove_legacy(self.ctx())
        self.assertEqual(sorted(removed["removed"]), sorted(setup.LEGACY_PLUGINS))
        doc = tomllib.loads(self.config_text())
        self.assertNotIn("ctr-local", doc.get("marketplaces", {}))
        self.assertFalse([k for k in doc.get("hooks", {}).get("state", {}) if "ctr-local" in k])
        self.assertIn(PID, doc["plugins"])  # ours stays

    def test_old_kernel_is_incompatible_and_nothing_is_registered(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_VERSION": "0.150.0"}):
            result = setup.install(self.ctx())
        self.assertEqual(result["status"], compat.INCOMPATIBLE)
        self.assertIn("低于 Codex 助手包 支持的最低版本 0.163.0", result["message"])
        self.assertIn("已停用插件", result["message"])
        self.assertIn("「Codex 充值」", result["message"])
        self.assertNotIn("codex-companion", self.config_text())

    def test_kernel_without_plugin_command_is_incompatible(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_NO_PLUGIN": "1"}):
            result = setup.install(self.ctx())
        self.assertEqual(result["status"], compat.INCOMPATIBLE)
        self.assertIn("plugin 命令", result["message"])

    @unittest.skipUnless(shipped_plugin_has_hooks(),
                         "the shipped plugin has no hooks: that gate no longer applies")
    def test_hooks_that_do_not_load_mean_incompatible_and_the_plugin_is_removed(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_NO_HOOKS": "1"}):
            result = setup.install(self.ctx(), probe=False)
        self.assertEqual(result["status"], compat.INCOMPATIBLE)
        self.assertNotIn(PID, tomllib.loads(self.config_text()).get("plugins", {}))

    def test_newer_untested_kernel_installs_with_a_warning(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_VERSION": "0.170.1"}):
            result = setup.install(self.ctx())
        self.assertEqual(result["status"], compat.UNTESTED)
        self.assertIn("未经验证", result["message"])
        self.assertIn(PID, tomllib.loads(self.config_text())["plugins"])

    def test_no_codex(self):
        with mock.patch.dict(os.environ, {"CTR_CODEX_BIN": ""}), \
                mock.patch("ctr.kernel._bin_root", return_value=None):
            result = setup.install(self.ctx())
        self.assertEqual(result["status"], compat.NO_CODEX)
        self.assertIn("未检测到 Codex", result["message"])


class UninstallTest(CompanionCase):
    product_fields = {"sync_base_url": "http://127.0.0.1:9"}

    def setUp(self):
        super().setUp()
        (self.home / "config.toml").write_text(ORIGINAL, encoding="utf-8")
        setup.install(self.ctx())
        self.trust_all()

    def test_config_is_restored_exactly(self):
        result = setup.uninstall(self.ctx())
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["config"]["verified"])
        self.assertEqual(result["config"]["leftovers"], [])
        self.assertEqual(tomllib.loads(self.config_text()), tomllib.loads(ORIGINAL))
        self.assertEqual(self.config_text().strip(), ORIGINAL.strip())
        self.assertFalse((self.home / "plugins" / "cache" / "codex-companion").exists())

    def test_trust_entries_left_by_codex_are_removed(self):
        setup.uninstall(self.ctx())
        self.assertNotIn("codex-companion@codex-companion:", self.config_text())

    def test_recording_is_stopped_and_data_kept_by_default(self):
        from ctr import state as rstate
        from ctr import timeutil

        rstate.enable(self.rpaths.state, timeutil.now())
        sconfig.save(SyncPaths(self.rpaths), sconfig.Config(base_url="http://127.0.0.1:9"))
        sstate.enable(SyncPaths(self.rpaths), timeutil.now(), "http://127.0.0.1:9")
        result = setup.uninstall(self.ctx())
        self.assertFalse(rstate.is_recording(rstate.load(self.rpaths.state)))
        self.assertFalse(sstate.is_on(sstate.load(SyncPaths(self.rpaths))))
        self.assertIn(str(self.rpaths.root), result["kept"])

    def test_purge_removes_every_data_directory(self):
        from ctr import state as rstate
        from ctr import timeutil

        rstate.enable(self.rpaths.state, timeutil.now())
        (self.tmp / "cdkr").mkdir(exist_ok=True)
        setup.uninstall(self.ctx(), purge_local=True)
        for d in (self.rpaths.root, self.tmp / "cdkr", self.cpaths.root):
            self.assertFalse(d.exists(), d)

    def test_without_the_kernel_the_config_is_still_cleaned(self):
        with mock.patch.dict(os.environ, {"CTR_CODEX_BIN": ""}), \
                mock.patch("ctr.kernel._bin_root", return_value=None):
            result = setup.uninstall(self.ctx())
        self.assertEqual(result["kernel"], "missing")
        self.assertEqual(tomllib.loads(self.config_text()), tomllib.loads(ORIGINAL))

    def test_reinstall_after_uninstall_restores_the_plugin(self):
        setup.uninstall(self.ctx())
        result = setup.install(self.ctx())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hooks"]["trusted"], 0)  # a new install is trusted afresh by the user

    def test_stops_bundle_processes(self):
        with mock.patch("companion.procs.stop", return_value=["pythonw.exe"]) as stop:
            result = setup.uninstall(self.ctx())
        roots = stop.call_args[0][0]
        self.assertIn(self.home / "plugins" / "cache" / "codex-companion", roots)
        self.assertIn(self.install_dir, roots)
        self.assertEqual(result["stopped"], ["pythonw.exe"])


class DisabledByUserTest(CompanionCase):
    def setUp(self):
        super().setUp()
        (self.home / "config.toml").write_text(ORIGINAL, encoding="utf-8")
        setup.install(self.ctx())
        self.trust_all()
        from ctr import kernel

        self.exe = kernel.find_app_kernel()[0]
        self.assertTrue(setup._set_enabled(self.exe, PID, False))  # what the App's plugin switch does

    def test_update_keeps_the_plugin_disabled_and_is_not_incompatible(self):
        result = setup.install(self.ctx())
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["disabled_by_user"])
        self.assertIn("保持禁用", result["message"])
        doc = tomllib.loads(self.config_text())
        self.assertIs(doc["plugins"][PID]["enabled"], False)  # `plugin add` turned it on; we turned it back

    def test_no_uploader_is_started_for_a_disabled_plugin(self):
        from ctr import timeutil

        sp = SyncPaths(self.rpaths)
        sconfig.save(sp, sconfig.Config(base_url="http://127.0.0.1:9"))
        sstate.enable(sp, timeutil.now(), "http://127.0.0.1:9")
        self.spawn.reset_mock()
        setup.install(self.ctx())
        self.spawn.assert_not_called()


class RepairTest(CompanionCase):
    def test_repair_from_the_plugin_cache_uses_the_recorded_install_dir(self):
        setup.install(self.ctx())
        cached = self.home / "plugins/cache/codex-companion/codex-companion" / plugin_version()
        from companion import product as product_mod

        with mock.patch.dict(os.environ, {"COMPANION_DATA_DIR": str(self.cpaths.root)}):
            ctx = setup.Context.default(product_mod.load(cached))
        self.assertEqual(ctx.install_dir, self.install_dir)
        self.assertEqual(ctx.marketplace_dir, self.install_dir / "marketplace")
        from ctr import kernel

        kernel.run(kernel.find_app_kernel()[0], ["plugin", "remove", PID])  # e.g. removed in Codex
        result = setup.install(ctx)
        self.assertEqual(result["status"], "ok", result)
        root = ctx.state().marketplace_root
        self.assertEqual(os.path.normcase(root), os.path.normcase(str(self.install_dir / "marketplace")))

    def test_without_an_install_dir_repair_asks_for_the_installer(self):
        ctx = self.ctx()
        ctx.install_dir = None
        result = setup.install(ctx)
        self.assertEqual(result["status"], "failed")
        self.assertIn("重新运行安装程序", result["message"])


class OfflineUninstallTest(CompanionCase):
    product_fields = {"sync_base_url": "http://127.0.0.1:9"}  # nothing listens there

    def setUp(self):
        super().setUp()
        (self.home / "config.toml").write_text(ORIGINAL, encoding="utf-8")
        setup.install(self.ctx())
        from ctr import timeutil
        from ctr.sync import vault

        sp = SyncPaths(self.rpaths)
        sconfig.save(sp, sconfig.Config(base_url="http://127.0.0.1:9"))
        sstate.enable(sp, timeutil.now(), "http://127.0.0.1:9")
        vault.save(sp.device, {"device_id": "d1", "device_token": "ctrd_x", "base_url": "http://127.0.0.1:9"})
        (self.rpaths.root / "sessions" / "2026-10-29").mkdir(parents=True)
        (self.rpaths.root / "sessions" / "2026-10-29" / "s.jsonl").write_text("{}\n", encoding="utf-8")

    def test_purge_keeps_what_the_unsent_revocation_needs(self):
        from ctr.sync import vault

        result = setup.uninstall(self.ctx(), delete_remote=True, purge_local=True)
        self.assertFalse(result["remote"]["sent"])
        sp = SyncPaths(self.rpaths)
        self.assertEqual(result["kept"], [str(sp.root)])
        self.assertIn(sstate.OP_DELETE_REMOTE, sstate.load(sp).pending_ops)
        self.assertIsNotNone(vault.load(sp.device))
        self.assertFalse((self.rpaths.root / "sessions").exists())  # the recordings themselves are gone
        self.assertFalse((self.tmp / "cdkr").exists())

    def test_the_kept_request_is_sent_after_reinstalling(self):
        setup.uninstall(self.ctx(), delete_remote=True, purge_local=True)
        self.spawn.reset_mock()
        setup.install(self.ctx())
        self.spawn.assert_called()  # the uploader runs pending operations before anything else


class BackupTest(CompanionCase):
    def test_backups_are_removed_once_the_config_is_restored(self):
        (self.home / "config.toml").write_text(ORIGINAL, encoding="utf-8")
        setup.install(self.ctx())
        self.assertTrue(any(self.cpaths.backup.iterdir()))
        result = setup.uninstall(self.ctx())
        self.assertTrue(result["config"]["verified"])
        self.assertFalse(self.cpaths.backup.exists())


class PrepareUpgradeTest(CompanionCase):
    def test_only_long_lived_processes_are_stopped(self):
        with mock.patch("companion.procs.stop", return_value=[]) as stop:
            setup.prepare_upgrade(self.ctx())
        self.assertEqual(stop.call_args[1]["names"], {"pythonw.exe"})


class ContextTest(CompanionCase):
    def test_install_dir_is_derived_from_the_plugin_location(self):
        self.assertEqual(setup._install_dir_of(self.root), self.install_dir)
        self.assertIsNone(setup._install_dir_of(self.home / "plugins/cache/codex-companion/codex-companion" / plugin_version()))


if __name__ == "__main__":
    unittest.main()
