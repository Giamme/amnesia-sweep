from __future__ import annotations

import os
import unittest

from amnesia_sweep.sizing import changed, measure
from tests.fixtures import SandboxTestCase, touch


def disk_usage(*paths: str) -> int:
    """Independent oracle: st_blocks * 512 of each listed path, nothing followed."""
    return sum(os.lstat(p).st_blocks * 512 for p in paths)


class MeasureTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.root, "measured")
        self.small = touch(os.path.join(self.dir, "small.txt"), 10)
        self.big = touch(os.path.join(self.dir, "sub", "big.bin"), 100_000)

    def test_bytes_are_the_disk_blocks_of_every_entry_including_directories(self):
        part = measure(self.dir)
        expected = disk_usage(self.dir, self.small, os.path.dirname(self.big), self.big)
        self.assertEqual(part.bytes, expected)
        self.assertEqual(part.files, 2)
        self.assertFalse(part.missing)
        self.assertEqual(part.errors, 0)

    def test_symlinks_to_big_outside_files_and_folders_are_not_followed_or_counted(self):
        outside_file = touch(os.path.join(self.outside, "huge.bin"), 2_000_000)
        touch(os.path.join(self.outside, "tree", "inner.bin"), 2_000_000)
        os.symlink(outside_file, os.path.join(self.dir, "link-to-file"))
        os.symlink(os.path.join(self.outside, "tree"), os.path.join(self.dir, "link-to-dir"))

        part = measure(self.dir)

        expected = disk_usage(self.dir, self.small, os.path.dirname(self.big), self.big,
                              os.path.join(self.dir, "link-to-file"), os.path.join(self.dir, "link-to-dir"))
        self.assertEqual(part.bytes, expected)
        self.assertLess(part.bytes, 1_000_000)
        self.assertEqual(part.files, 4)  # two files plus the two links themselves

    def test_a_symlink_measured_directly_is_the_link_not_its_target(self):
        target = touch(os.path.join(self.outside, "huge.bin"), 2_000_000)
        link = os.path.join(self.root, "link")
        os.symlink(target, link)
        part = measure(link)
        self.assertEqual(part.files, 1)
        self.assertLess(part.bytes, 1_000_000)

    def test_a_hard_linked_file_is_counted_once(self):
        os.link(self.big, os.path.join(self.dir, "big-again.bin"))
        part = measure(self.dir)
        self.assertEqual(part.bytes, disk_usage(self.dir, self.small, os.path.dirname(self.big), self.big))
        self.assertEqual(part.files, 2)

    def test_a_missing_path_is_reported_missing_with_nothing_counted(self):
        part = measure(os.path.join(self.root, "nope"))
        self.assertTrue(part.missing)
        self.assertEqual((part.bytes, part.files, part.errors), (0, 0, 0))

    @unittest.skipIf(os.geteuid() == 0, "root can read a chmod 000 folder")
    def test_an_unreadable_subfolder_is_counted_as_an_error_not_a_crash(self):
        locked = os.path.join(self.dir, "locked")
        touch(os.path.join(locked, "secret"), 10)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        part = measure(self.dir)
        self.assertEqual(part.errors, 1)
        self.assertEqual(part.files, 2)  # the readable files are still counted


class ChangedTests(SandboxTestCase):
    STAMP = 1_700_000_000

    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.root, "archived")
        self.file = touch(os.path.join(self.dir, "a.txt"), 5000, mtime=self.STAMP)
        self.settle()
        self.recorded = measure(self.dir).fingerprint()

    def settle(self):
        """Put the folder's own mtime back so only the change under test differs."""
        os.utime(self.dir, (self.STAMP, self.STAMP))

    def test_an_untouched_path_is_not_changed(self):
        self.assertFalse(changed(self.recorded, measure(self.dir)))

    def test_appending_bytes_counts_as_changed(self):
        with open(self.file, "ab") as handle:
            handle.write(b"y" * 8192)
        os.utime(self.file, (self.STAMP, self.STAMP))
        self.assertTrue(changed(self.recorded, measure(self.dir)))

    def test_a_new_file_counts_as_changed(self):
        touch(os.path.join(self.dir, "new.txt"), 0, mtime=self.STAMP)
        self.settle()
        self.assertTrue(changed(self.recorded, measure(self.dir)))

    def test_a_newer_mtime_with_the_same_size_counts_as_changed(self):
        os.utime(self.file, (self.STAMP + 100, self.STAMP + 100))
        self.assertTrue(changed(self.recorded, measure(self.dir)))

    def test_a_vanished_path_counts_as_changed(self):
        self.assertTrue(changed(self.recorded, measure(os.path.join(self.root, "gone"))))


if __name__ == "__main__":
    unittest.main()
