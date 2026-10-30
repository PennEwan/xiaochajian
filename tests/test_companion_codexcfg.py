"""Reading config.toml for the plugin's state, and removing exactly our tables on uninstall."""
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from companion import codexcfg
from ctr.sync import hostgate

PID, MKT = "codex-companion@codex-companion", "codex-companion"
REPO = Path(__file__).resolve().parents[1]

CONFIG = '''model = "gpt-6-luna"
notify = [
  "C:\\\\x\\\\notify.exe",
  ["nested"],
]

[marketplaces.ctr-local]
source_type = "local"
source = '\\\\?\\D:\\codex-task-recorder'

[marketplaces.codex-companion]
source_type = "local"
source = '\\\\?\\C:\\Users\\u\\AppData\\Local\\Programs\\CodexCompanion\\marketplace'

[plugins."codex-task-recorder@ctr-local"]
enabled = true

[plugins."codex-companion@codex-companion"]
enabled = true

[projects.'d:\\codexrouter']
trust_level = "trusted"

[hooks.state."codex-task-recorder@ctr-local:hooks/hooks.json:stop:0:0"]
trusted_hash = "sha256:aa"

[hooks.state."codex-companion@codex-companion:hooks/hooks.json:stop:0:0"]
trusted_hash = "sha256:bb"

[hooks.state."codex-companion@codex-companion:hooks/hooks.json:user_prompt_submit:0:0"]
trusted_hash = "sha256:cc"
enabled = false

[[skills.extra]]
path = "x"
'''


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="companion-cfg-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cfg = self.home / "config.toml"
        self.cfg.write_text(CONFIG, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class StripTest(Case):
    def test_removes_only_our_tables(self):
        text, removed = codexcfg.strip_tables(CONFIG, PID, MKT)
        self.assertEqual(len(removed), 4)
        doc = tomllib.loads(text)
        self.assertEqual(doc, codexcfg.without_ours(tomllib.loads(CONFIG), PID, MKT))
        self.assertIn("codex-task-recorder@ctr-local", doc["plugins"])
        self.assertIn("ctr-local", doc["marketplaces"])
        self.assertEqual(doc["notify"][1], ["nested"])
        self.assertEqual(codexcfg.leftovers(doc, PID, MKT), [])

    def test_header_parsing_follows_toml_quoting(self):
        self.assertEqual(codexcfg._parse_header('[hooks.state."a@b:hooks/hooks.json:stop:0:0"]'),
                         ["hooks", "state", "a@b:hooks/hooks.json:stop:0:0"])
        self.assertEqual(codexcfg._parse_header("[projects.'d:\\x']"), ["projects", "d:\\x"])
        self.assertEqual(codexcfg._parse_header("[[skills.extra]]"), ["skills", "extra"])
        self.assertIsNone(codexcfg._parse_header('["nested"],'))
        self.assertIsNone(codexcfg._parse_header("model = 1"))


class CleanTest(Case):
    def test_clean_verifies_and_keeps_a_backup(self):
        result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertTrue(result.verified)
        self.assertEqual(len(result.removed), 4)
        self.assertEqual(Path(result.backup).read_text(encoding="utf-8"), CONFIG)
        self.assertEqual(codexcfg.leftovers(codexcfg.read(self.home), PID, MKT), [])

    def test_nothing_of_ours_leaves_the_file_untouched(self):
        self.cfg.write_text('model = "x"\n', encoding="utf-8")
        result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertTrue(result.verified)
        self.assertEqual(result.removed, [])
        self.assertIsNone(result.backup)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), 'model = "x"\n')

    def test_keys_written_in_another_form_are_reported_not_mangled(self):
        odd = 'plugins."codex-companion@codex-companion".enabled = true\nmodel = "x"\n'
        self.cfg.write_text(odd, encoding="utf-8")
        result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertFalse(result.verified)
        self.assertEqual(result.error, "not_table_form")
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), odd)

    def test_sub_tables_of_ours_are_ours_too(self):
        odd = CONFIG.replace('[plugins."codex-companion@codex-companion"]\nenabled = true\n',
                             '[plugins."codex-companion@codex-companion"]\nenabled = true\n'
                             '[plugins."codex-companion@codex-companion".x]\ny = 1\n')
        self.cfg.write_text(odd, encoding="utf-8")
        result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertTrue(result.verified)
        self.assertEqual(codexcfg.leftovers(codexcfg.read(self.home), PID, MKT), [])

    def test_an_edit_that_would_change_other_settings_is_refused_and_rolled_back(self):
        # A multi-line string that happens to contain a line looking like one of our headers:
        # stripping it would change another setting, so the verification refuses the edit.
        odd = CONFIG + ('\n[profiles.note]\ntext = """\n[plugins."codex-companion@codex-companion"]\n'
                        'enabled = true\n"""\n')
        self.cfg.write_text(odd, encoding="utf-8")
        result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertFalse(result.verified)
        self.assertEqual(result.error, "verify_failed")
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), odd)  # untouched
        self.assertFalse((self.home / "config.toml.companion-tmp").exists())

    def test_blank_lines_inside_multiline_strings_survive(self):
        text = CONFIG + '\n[profiles.note]\ntext = """\nline one\n\n\n\nline two\n"""\n'
        self.cfg.write_text(text, encoding="utf-8")
        self.assertTrue(codexcfg.clean(self.home, PID, MKT, self.tmp / "backup").verified)
        self.assertEqual(tomllib.loads(self.cfg.read_text(encoding="utf-8"))["profiles"]["note"]["text"],
                         "line one\n\n\n\nline two\n")

    def test_comments_above_the_next_table_are_kept(self):
        text = ('model = "x"\n\n[plugins."codex-companion@codex-companion"]\nenabled = true\n\n'
                '# my MCP server, keep\n[mcp_servers.mine]\ncommand = "x"\n')
        self.cfg.write_text(text, encoding="utf-8")
        self.assertTrue(codexcfg.clean(self.home, PID, MKT, self.tmp / "backup").verified)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"),
                         'model = "x"\n\n# my MCP server, keep\n[mcp_servers.mine]\ncommand = "x"\n')

    def test_an_explicit_parent_table_does_not_block_the_cleanup(self):
        text = ('[hooks.state]\n\n[hooks.state."codex-companion@codex-companion:hooks/hooks.json:stop:0:0"]\n'
                'trusted_hash = "sha256:1"\n')
        self.cfg.write_text(text, encoding="utf-8")
        result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertTrue(result.verified, result.error)
        self.assertEqual(codexcfg.leftovers(codexcfg.read(self.home), PID, MKT), [])

    def test_legacy_cleanup_can_leave_marketplace_tables_to_codex(self):
        result = codexcfg.clean(self.home, PID, None, self.tmp / "backup")
        self.assertTrue(result.verified)
        doc = codexcfg.read(self.home)
        self.assertIn("codex-companion", doc["marketplaces"])  # kept: Codex's own command removes it
        self.assertNotIn(PID, doc["plugins"])

    def test_undecodable_config_is_reported_not_raised(self):
        self.cfg.write_bytes(b'model = "\xff\xfe"\n')
        result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertEqual(result.error, "unparsable_before")

    def test_a_locked_config_is_reported_not_raised(self):
        from unittest import mock

        with mock.patch("companion.codexcfg.replace", side_effect=PermissionError(13, "locked")):
            result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertFalse(result.verified)
        self.assertEqual(result.error, "PermissionError")
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), CONFIG)

    def test_unparsable_config_is_left_alone(self):
        self.cfg.write_text("this is [not toml", encoding="utf-8")
        result = codexcfg.clean(self.home, PID, MKT, self.tmp / "backup")
        self.assertEqual(result.error, "unparsable_before")
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), "this is [not toml")

    def test_missing_config(self):
        self.cfg.unlink()
        self.assertTrue(codexcfg.clean(self.home, PID, MKT, self.tmp / "backup").verified)


class PluginStateTest(Case):
    def setUp(self):
        super().setUp()
        # The shipped companion plugin has no hooks any more; the hook machinery still belongs to
        # the recorder plugin, so the fixture is built from that one.
        self.root = self.tmp / "plugin"
        shutil.copytree(REPO / "plugins" / "codex-task-recorder" / ".codex-plugin",
                        self.root / ".codex-plugin")
        shutil.copytree(REPO / "plugins" / "codex-task-recorder" / "hooks", self.root / "hooks")

    def test_counts_and_reason(self):
        st = codexcfg.plugin_state(self.home, self.root, PID, MKT)
        self.assertTrue(st.installed and st.enabled)
        self.assertEqual((st.hooks_total, st.hooks_trusted, st.hooks_off), (12, 2, 1))
        self.assertEqual(st.reason, hostgate.HOOK_OFF)
        self.assertTrue(st.marketplace_root.endswith("CodexCompanion\\marketplace"))
        self.assertFalse(st.marketplace_root.startswith("\\\\?\\"))

    def test_installed_plugins_of_a_marketplace(self):
        self.assertEqual(codexcfg.installed_plugins(self.home, "ctr-local"), ["codex-task-recorder@ctr-local"])

    def test_unreadable(self):
        self.cfg.write_text("[[[", encoding="utf-8")
        st = codexcfg.plugin_state(self.home, self.root, PID, MKT)
        self.assertFalse(st.readable)
        self.assertEqual(st.reason, hostgate.UNREADABLE)


if __name__ == "__main__":
    unittest.main()
