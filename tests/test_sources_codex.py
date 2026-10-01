from __future__ import annotations

import os
import unittest

from amnesia_sweep.liveness import Liveness
from amnesia_sweep.sources import codex
from tests.fixtures import (FakeRunner, SandboxTestCase, by_id, leaf_owning, make_codex, make_rollout,
                            paths_of)

A = "0199a1b2-c3d4-7e5f-8a9b-000000000001"
B = "0199a1b2-c3d4-7e5f-8a9b-000000000002"
C = "0199a1b2-c3d4-7e5f-8a9b-000000000003"


class CodexTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.paths = make_codex(self.env, releases=("0.40.0", "0.41.0", "0.42.0"), current="0.41.0")
        self.source = codex.sources()[0]

    def test_only_the_release_current_points_at_is_kept(self):
        root = self.scan_source(self.source)
        self.assertEqual(leaf_owning(root, self.paths["0.41.0"]).reason, "current version")
        self.assertFalse(leaf_owning(root, self.paths["0.41.0"]).actionable)
        self.assertTrue(leaf_owning(root, self.paths["0.40.0"]).actionable)
        self.assertTrue(leaf_owning(root, self.paths["0.42.0"]).actionable)

    def test_a_release_a_running_process_mentions_is_kept(self):
        args = f"{self.paths['0.40.0']}/codex app-server --listen"
        liveness = Liveness(runner=FakeRunner(), process_args=[args])
        root = self.scan_source(self.source, liveness=liveness)
        self.assertEqual(leaf_owning(root, self.paths["0.40.0"]).reason, "a running process uses it")
        self.assertTrue(leaf_owning(root, self.paths["0.42.0"]).actionable)

    def test_rollouts_are_grouped_by_the_cwd_on_their_first_line(self):
        proj = os.path.join(self.home, "dev", "proj")
        os.makedirs(proj)
        first = make_rollout(self.env, A, proj, day="2026/08/01")
        second = make_rollout(self.env, B, proj, day="2026/08/02")
        other = make_rollout(self.env, C, "/nowhere/else", day="2026/08/01")
        root = self.scan_source(self.source)
        project = by_id(root)[leaf_owning(root, first).id.rsplit("/", 1)[0]]
        self.assertEqual(project.label, "~/dev/proj")
        self.assertEqual(paths_of(project), {first, second})
        self.assertNotIn("orphan", project.flags)
        elsewhere = by_id(root)[leaf_owning(root, other).id.rsplit("/", 1)[0]]
        self.assertIn("orphan", elsewhere.flags)

    def test_databases_auth_and_config_are_never_actionable(self):
        root = self.scan_source(self.source)
        for name in ("sqlite", "auth", "config"):
            with self.subTest(name):
                self.assertFalse(leaf_owning(root, self.paths[name]).actionable)


if __name__ == "__main__":
    unittest.main()
