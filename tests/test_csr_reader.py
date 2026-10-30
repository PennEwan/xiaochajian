import os
import unittest

from csr import reader

from csr_helpers import SID, SID2, TempDirCase, file_name


class DiscoverTest(TempDirCase):
    def test_lists_only_rollout_files_in_the_given_directory(self):
        self.write(b"", SID)
        (self.sessions / "auth.json").write_text("{}")
        (self.sessions / "notes.jsonl").write_text("{}\n")
        (self.sessions / "rollout-x.jsonl.zst").write_bytes(b"")
        sub = self.sessions / "2026" / "10"
        sub.mkdir(parents=True)
        (sub / file_name(SID2)).write_bytes(b"")

        self.assertEqual([p.name for p in reader.discover(self.sessions)], [file_name(SID)])

    def test_recursive_includes_subdirectories(self):
        self.write(b"", SID)
        sub = self.sessions / "2026" / "10"
        sub.mkdir(parents=True)
        (sub / file_name(SID2)).write_bytes(b"")

        names = sorted(p.name for p in reader.discover(self.sessions, recursive=True))
        self.assertEqual(names, sorted([file_name(SID), file_name(SID2)]))

    def test_session_filter_matches_id_prefix_without_opening_other_files(self):
        self.write(b"", SID)
        self.write(b"", SID2)
        self.assertEqual([p.name for p in reader.discover(self.sessions, sessions=["01a18266-5a69"])],
                         [file_name(SID)])

    def test_session_id_from_file_name(self):
        self.assertEqual(reader.session_id_from_name(file_name(SID)), SID)
        self.assertIsNone(reader.session_id_from_name("rollout-broken.jsonl"))

    def test_skips_links_that_point_outside_the_directory(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        target = outside / file_name(SID2)
        target.write_bytes(b"")
        try:
            os.symlink(target, self.sessions / file_name(SID2))
        except (OSError, NotImplementedError):
            self.skipTest("creating symlinks needs extra privileges here")
        self.assertEqual(reader.discover(self.sessions), [])


class ReadLinesTest(TempDirCase):
    def test_returns_complete_lines_with_offsets(self):
        path = self.write(b'{"a":1}\n\n{"b":2}\n')
        chunk = reader.read_complete_lines(path, 0)
        self.assertEqual(chunk.lines, [(0, b'{"a":1}'), (9, b'{"b":2}')])
        self.assertEqual(chunk.end, 17)
        self.assertEqual(chunk.pending, 0)
        self.assertTrue(chunk.eof)

    def test_partial_tail_is_not_consumed_and_is_resumed_later(self):
        path = self.write(b'{"a":1}\n{"b":')
        first = reader.read_complete_lines(path, 0)
        self.assertEqual(first.lines, [(0, b'{"a":1}')])
        self.assertEqual(first.end, 8)
        self.assertEqual(first.pending, 5)

        self.append(b'2}\n')
        second = reader.read_complete_lines(path, first.end)
        self.assertEqual(second.lines, [(8, b'{"b":2}')])
        self.assertEqual(second.pending, 0)

    def test_reads_in_bounded_chunks_but_never_splits_a_line(self):
        path = self.write(b'{"a":"' + b"x" * 100 + b'"}\n{"b":2}\n')
        chunk = reader.read_complete_lines(path, 0, max_bytes=10)
        self.assertEqual(len(chunk.lines), 1)
        self.assertFalse(chunk.eof)
        rest = reader.read_complete_lines(path, chunk.end, max_bytes=10)
        self.assertEqual(rest.lines, [(chunk.end, b'{"b":2}')])
        self.assertTrue(rest.eof)

    def test_skips_utf8_bom_at_start(self):
        path = self.write(b'\xef\xbb\xbf{"a":1}\n')
        self.assertEqual(reader.read_complete_lines(path, 0).lines, [(3, b'{"a":1}')])

    def test_head_fingerprint_changes_when_file_is_replaced(self):
        path = self.write(b'{"a":1}\n')
        before = reader.head_sha(path, 8)
        self.write(b'{"a":2}\n')
        self.assertNotEqual(before, reader.head_sha(path, 8))


@unittest.skipUnless(os.name == "nt", "Windows share modes")
class SharedOpenTest(TempDirCase):
    def test_file_can_be_renamed_and_deleted_while_open(self):
        path = self.write(b'{"a":1}\n')
        with reader.open_shared(path) as f:
            moved = path.with_name("moved.jsonl")
            os.replace(path, moved)
            os.remove(moved)
            self.assertEqual(f.read(), b'{"a":1}\n')

    def test_handle_is_read_only(self):
        path = self.write(b'{"a":1}\n')
        with reader.open_shared(path) as f:
            with self.assertRaises(OSError):
                f.write(b"x")
        self.assertEqual(path.read_bytes(), b'{"a":1}\n')


if __name__ == "__main__":
    unittest.main()
