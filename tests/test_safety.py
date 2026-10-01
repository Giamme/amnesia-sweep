from __future__ import annotations

import os
import unittest

from amnesia_sweep.model import Part
from amnesia_sweep.safety import Guard
from tests.fixtures import SandboxTestCase, touch


class GuardTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.claude = self.env.claude_dir
        self.project = os.path.join(self.claude, "projects", "-x-proj")
        self.transcript = touch(os.path.join(self.project, "s1.jsonl"), 10)
        self.memory = os.path.dirname(touch(os.path.join(self.project, "memory", "MEMORY.md"), 10))
        touch(os.path.join(self.outside, "precious", "file"), 10)
        os.symlink(os.path.join(self.outside, "precious"), os.path.join(self.claude, "link"))
        self.guard = Guard(self.env, roots=[self.claude],
                           protected=[os.path.join(self.claude, "projects", "*", "memory")])

    def check(self, path: str):
        return self.guard.check(Part(path=path))

    def test_a_normal_leaf_inside_a_root_is_allowed(self):
        self.assertIsNone(self.check(self.transcript))

    def test_dangerous_paths_are_refused_for_the_right_reason(self):
        cases = (
            ("home", self.home, "refusing to delete a top-level or system folder"),
            ("filesystem root", "/", "path is too short"),
            ("a source root itself", self.claude, "refusing to delete a top-level or system folder"),
            ("outside every root", os.path.join(self.outside, "precious"), "outside the known agent folders"),
            ("through a symlinked parent", os.path.join(self.claude, "link", "file"),
             "outside the known agent folders"),
            ("a folder containing a protected path", self.project, "contains a protected path"),
            ("a protected path itself", self.memory, "protected"),
            ("a relative path", "home/.claude/projects", "not a clean absolute path"),
            ("a path with ..", self.claude + "/projects/../projects/-x-proj", "not a clean absolute path"),
        )
        for name, path, reason in cases:
            with self.subTest(name):
                refusal = self.check(path) or ""
                self.assertTrue(refusal.startswith(reason), f"{path}: {refusal!r}")

    # Regression: the guard used to check only the path itself, so a file inside a protected folder
    # (here projects/*/memory/MEMORY.md) passed it.
    def test_a_file_inside_a_protected_folder_is_refused(self):
        self.assertIsNotNone(self.check(os.path.join(self.memory, "MEMORY.md")))


if __name__ == "__main__":
    unittest.main()
