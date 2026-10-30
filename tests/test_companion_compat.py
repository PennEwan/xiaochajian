"""Codex version verdicts, product.json, and finding / stopping the bundle's own processes."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from companion import compat, procs, product


class CompatTest(unittest.TestCase):
    p = product.Product(root=Path("."), min_kernel="0.163.0", tested_kernels=("0.163.0-alpha.10.2",))

    def test_verdicts(self):
        cases = [("0.163.0-alpha.10.2", [], compat.OK), ("0.164.0", [], compat.UNTESTED),
                 ("0.163.1-alpha.1", [], compat.UNTESTED), ("0.162.9", [], compat.INCOMPATIBLE),
                 ("0.170.0", ["hooks/list"], compat.INCOMPATIBLE), (None, [], compat.UNKNOWN)]
        for version, problems, expected in cases:
            c = compat.evaluate(self.p, version, "26.1", "C:\\codex.exe", problems)
            self.assertEqual(c.status, expected, version)

    def test_unreadable_version_changes_nothing(self):
        c = compat.evaluate(self.p, None, kernel_path="C:\codex.exe")
        self.assertEqual(c.status, compat.UNKNOWN)
        self.assertTrue(c.can_record)  # a timeout must not switch anything off
        self.assertIn("没有做任何改动", c.message)

    def test_no_codex(self):
        c = compat.evaluate(self.p, None)
        self.assertEqual(c.status, compat.NO_CODEX)
        self.assertFalse(c.can_record)

    def test_degraded_message_says_what_still_works(self):
        msg = compat.evaluate(self.p, "0.150.0", kernel_path="x").message
        for part in ("0.150.0", "0.163.0", "不可用", "「Codex 充值」", "重新检测"):
            self.assertIn(part, msg)


class ProductTest(unittest.TestCase):
    def test_defaults_without_product_json(self):
        p = product.load(Path(tempfile.gettempdir()) / "no-such-plugin")
        self.assertEqual((p.plugin_id, p.version, p.sync_base_url), ("codex-companion@codex-companion", "0.0.0-dev", None))

    def test_product_json(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "product.json").write_text(json.dumps({
                "version": "1.2.3", "plugin_id": "a@b", "sync_base_url": "https://s", "recharge_base_url": " ",
                "compat": {"min_kernel": "0.170.0", "tested_kernels": ["0.170.1"]}}), encoding="utf-8")
            p = product.load(tmp)
            self.assertEqual((p.version, p.plugin, p.marketplace, p.sync_base_url, p.recharge_base_url),
                             ("1.2.3", "a", "b", "https://s", None))
            self.assertEqual((p.min_kernel, p.tested_kernels), ("0.170.0", ("0.170.1",)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(os.name == "nt", "Windows processes")
class ProcsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="companion-procs-"))
        src = Path(sys.base_prefix)
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        for name in ("python.exe", "pythonw.exe", "python312.dll", "python3.dll", "vcruntime140.dll",
                     "vcruntime140_1.dll"):
            if (src / name).exists():
                shutil.copy2(src / name, self.runtime / name)
        (self.runtime / "python312._pth").write_text(f"{src / 'Lib'}\n{src / 'DLLs'}\n", encoding="ascii")

    def tearDown(self):
        for pid, _ in procs.find([self.tmp]):
            procs.terminate(pid)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_find_and_stop_only_processes_under_the_roots(self):
        mine = subprocess.Popen([str(self.runtime / "pythonw.exe"), "-c", "import time; time.sleep(60)"])
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            deadline = time.monotonic() + 10
            while not procs.find([self.tmp]) and time.monotonic() < deadline:
                time.sleep(0.1)
            found = procs.find([self.tmp])
            self.assertEqual([pid for pid, _ in found], [mine.pid])
            self.assertEqual(procs.stop([self.tmp]), ["pythonw.exe"])
            self.assertIsNotNone(mine.wait(timeout=5))
            self.assertIsNone(other.poll())  # the user's own Python is left alone
        finally:
            other.kill()
            other.wait()
            mine.kill()

    def test_never_the_calling_process(self):
        self.assertEqual(procs.find([Path(sys.executable).parent], exclude={os.getpid()})
                         and [p for p, _ in procs.find([Path(sys.executable).parent]) if p == os.getpid()], [])


if __name__ == "__main__":
    unittest.main()
