from __future__ import annotations

import os
import time
import unittest

from amnesia_sweep.sources import temp
from tests.fixtures import OLD, SandboxTestCase, age_tree, dead_pid, leaf_owning, paths_of, touch


class TempTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.source = temp.sources()[0]

    def test_only_allowlisted_tmpdir_entries_are_listed_and_only_once_old_enough(self):
        tmpdir = self.env.tmpdir
        old_agent = os.path.dirname(touch(os.path.join(tmpdir, "codex-clipboard-abc", "img.png"), 10))
        fresh_agent = os.path.dirname(touch(os.path.join(tmpdir, "claude-fresh", "x"), 10))
        apple = os.path.dirname(touch(os.path.join(tmpdir, "com.apple.mdworker", "cache"), 10))
        stranger = os.path.dirname(touch(os.path.join(tmpdir, "some-app-cache", "x"), 10))
        for path in (old_agent, apple, stranger):
            age_tree(path, OLD)
        age_tree(fresh_agent, time.time() - 3 * 3600)  # past the grace period, under claude-*'s 24h

        root = self.scan_source(self.source)

        self.assertTrue(leaf_owning(root, old_agent).actionable)
        self.assertFalse(leaf_owning(root, fresh_agent).actionable)
        self.assertEqual(leaf_owning(root, fresh_agent).reason, "newer than 24h")
        self.assertNotIn(apple, paths_of(root))
        self.assertNotIn(stranger, paths_of(root))

    def test_sockets_of_dead_processes_are_actionable_and_live_ones_are_kept(self):
        sockets = os.path.join(self.env.tmp_root, "cc-socks")
        dead = touch(os.path.join(sockets, f"{dead_pid()}.sock"), mtime=OLD)
        live = touch(os.path.join(sockets, f"{os.getpid()}.sock"), mtime=OLD)
        root = self.scan_source(self.source)
        stale = leaf_owning(root, dead)
        self.assertEqual({p.path for p in stale.parts}, {dead})
        self.assertTrue(stale.actionable)
        self.assertNotIn(live, paths_of(root))


if __name__ == "__main__":
    unittest.main()
