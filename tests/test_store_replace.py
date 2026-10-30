import os
import threading
import unittest

from ctr import store

from helpers import TempPathsCase


class ReplaceTest(TempPathsCase):
    """Windows refuses os.replace while another handle has the target open; the uploader and
    `ctr sync status` read cursor and state files that hooks and the CLI replace."""

    def setUp(self):
        super().setUp()
        self.dst = self.tmp / "cursor.json"
        self.dst.write_text("old", encoding="utf-8")
        self.src = self.tmp / "cursor.tmp"
        self.src.write_text("new", encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_plain_replace_fails_while_a_reader_has_the_file_open(self):
        with open(self.dst, encoding="utf-8"):
            with self.assertRaises(PermissionError):
                os.replace(self.src, self.dst)

    def test_replace_waits_for_a_short_reader(self):
        reader = open(self.dst, encoding="utf-8")
        threading.Timer(0.1, reader.close).start()
        store.replace(self.src, self.dst)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()
