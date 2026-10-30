import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cdkr import cli, config

# Trimmed real output of `codex plugin list` (App kernel 0.163.0-alpha.10.2).
LISTING = """Marketplace `openai-bundled`
C:\\Users\\user\\.codex\\.tmp\\bundled-marketplaces\\openai-bundled\\.agents\\plugins\\marketplace.json

PLUGIN                               STATUS              VERSION        SOURCE
browser@openai-bundled               installed, enabled  26.1013.40934  C:\\x
chrome@openai-bundled                not installed                      C:\\y

Marketplace `ctr-local`
D:\\codex-task-recorder\\.agents\\plugins\\marketplace.json

PLUGIN                               STATUS              VERSION        SOURCE
codex-task-recorder@ctr-local        installed, enabled  0.1.0          D:\\codex-task-recorder\\plugins\\codex-task-recorder
codex-cdk-recharge@ctr-local         not installed                      D:\\codex-task-recorder\\plugins\\codex-cdk-recharge
"""


class ListingTest(unittest.TestCase):
    def test_installed_plugins_of_a_marketplace(self):
        self.assertEqual(cli.installed_from(LISTING, "ctr-local"), ["codex-task-recorder"])
        self.assertEqual(cli.installed_from(LISTING, "openai-bundled"), ["browser"])
        self.assertEqual(cli.installed_from("", "ctr-local"), [])

    def test_disabled_counts_as_installed(self):
        text = LISTING.replace("installed, enabled  0.1.0", "installed, disabled 0.1.0")
        self.assertEqual(cli.installed_from(text, "ctr-local"), ["codex-task-recorder"])


class UninstallTest(unittest.TestCase):
    def run_uninstall(self, listing_code):
        calls = []

        def fake_run(exe, args):
            calls.append(args)
            listing = LISTING.replace("codex-task-recorder@ctr-local        installed",
                                      "codex-task-recorder@ctr-local        not installed")
            return mock.Mock(returncode=listing_code if args == ["plugin", "list"] else 0,
                             stdout=listing if args == ["plugin", "list"] else "", stderr="")

        tmp = Path(tempfile.mkdtemp(prefix="cdkr-uninstall-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(cli, "_kernel", return_value=Path("codex.exe")),                 mock.patch.object(cli, "_run", fake_run), redirect_stdout(io.StringIO()):
            cli.main(["uninstall", "--yes"], paths=config.Paths(tmp / "data"))
        return calls

    def test_removes_marketplace_when_nothing_else_is_installed(self):
        self.assertIn(["plugin", "marketplace", "remove", "ctr-local"], self.run_uninstall(0))

    def test_keeps_marketplace_when_plugin_list_fails(self):
        self.assertNotIn(["plugin", "marketplace", "remove", "ctr-local"], self.run_uninstall(1))


class ConfigCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cdkr-cli-"))
        self.paths = config.Paths(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv), paths=self.paths)
        return code, out.getvalue()

    def test_sets_https_url(self):
        code, _ = self.run_cli("config", "--base-url", "https://pay.example.com/")
        self.assertEqual(code, 0)
        self.assertEqual(config.load(self.paths).base_url, "https://pay.example.com")

    def test_rejects_plain_http_remote_url(self):
        code, out = self.run_cli("config", "--base-url", "http://pay.example.com")
        self.assertEqual(code, 1)
        self.assertIn("https", out)
        self.assertIsNone(config.load(self.paths).base_url)

    def test_show_config_without_arguments(self):
        code, out = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn("未配置", out)
        self.run_cli("config", "--base-url", "http://127.0.0.1:18787")
        self.assertIn("http://127.0.0.1:18787（本机模拟后台）", self.run_cli("config")[1])

    def test_config_file_with_bom_is_read(self):
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.config.write_bytes(b"\xef\xbb\xbf" + b'{"base_url": "https://pay.example.com"}')
        self.assertEqual(config.load(self.paths).base_url, "https://pay.example.com")


if __name__ == "__main__":
    unittest.main()
