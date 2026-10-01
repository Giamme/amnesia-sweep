from __future__ import annotations

import os
import shutil
import unittest
from unittest import mock

from amnesia_sweep.actions import Executor, build_plan
from amnesia_sweep.archive import ArchiveStore
from amnesia_sweep.config import Config
from amnesia_sweep.model import Part
from amnesia_sweep.safety import Guard
from amnesia_sweep.worktrees import Git, Worktrees
from tests.fixtures import GIT, SandboxTestCase, git, leaf_owning, make_git_repo, paths_of, touch


@unittest.skipIf(GIT is None, "git is not installed")
class WorktreeTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = make_git_repo(os.path.join(self.home, "dev", "repo"))
        self.agent = self.add_worktree("claude/foo", os.path.join(self.repo, ".claude", "worktrees", "foo"))

    def add_worktree(self, branch: str, path: str) -> str:
        git(self.repo, "worktree", "add", "-q", "-b", branch, path)
        return os.path.realpath(path)

    def scan(self, **config):
        # A zero grace period: these worktrees were just created, and recency is not under test here.
        return self.scan_source(Worktrees(), config=Config(active_grace_minutes=0, **config))

    def listed(self) -> str:
        return git(self.repo, "worktree", "list", "--porcelain")

    def executor(self) -> Executor:
        return Executor(self.env, Guard(self.env, roots=[], protected=[], repos=[self.repo]),
                        ArchiveStore(self.env.state_dir))

    def test_an_agent_worktree_under_dot_claude_is_found_from_its_repo_and_classified_agent_made(self):
        leaf = leaf_owning(self.scan(), self.agent)
        self.assertTrue(leaf.meta.get("agent"))
        self.assertNotIn("not-agent", leaf.flags)
        self.assertEqual(leaf.meta["branch"], "claude/foo")
        self.assertEqual((leaf.parts[0].remover, leaf.parts[0].repo), ("git-worktree", self.repo))
        self.assertTrue(leaf.actionable)
        self.assertNotIn("dirty", leaf.flags)

    def test_a_plain_branch_outside_agent_paths_is_hidden_unless_all_worktrees_is_on(self):
        mine = self.add_worktree("feature", os.path.join(self.home, "dev", "repo-feature"))
        self.assertNotIn(mine, paths_of(self.scan()))
        leaf = leaf_owning(self.scan(all_worktrees=True), mine)
        self.assertIn("not-agent", leaf.flags)

    def test_the_main_checkout_is_never_listed(self):
        self.assertNotIn(self.repo, paths_of(self.scan(all_worktrees=True)))

    def test_editing_a_tracked_file_flags_the_worktree_dirty(self):
        touch(os.path.join(self.agent, "README.md"), data=b"changed\n")
        leaf = leaf_owning(self.scan(), self.agent)
        self.assertIn("dirty", leaf.flags)
        self.assertEqual(leaf.meta["dirty"], "1")

    def test_a_locked_worktree_is_not_actionable(self):
        git(self.repo, "worktree", "lock", "--reason", "agent busy", self.agent)
        leaf = leaf_owning(self.scan(), self.agent)
        self.assertFalse(leaf.actionable)
        self.assertEqual(leaf.reason, "locked: agent busy")

    def test_a_worktree_folder_deleted_by_hand_is_pruned_through_git(self):
        shutil.rmtree(self.agent)
        root = self.scan()
        leaf = leaf_owning(root, self.agent)
        self.assertEqual(leaf.parts[0].remover, "git-prune")
        self.assertIn("prunable", leaf.flags)

        results = self.executor().run(build_plan(root, {leaf.id: "delete"}))

        self.assertTrue(results[0].ok, results[0].error)
        self.assertNotIn(self.agent, self.listed())

    def test_check_removable_refuses_a_dirty_worktree_unless_forced(self):
        part = Part(path=self.agent, remover="git-worktree", repo=self.repo)
        self.assertIsNone(Git().check_removable(part, force=False))
        touch(os.path.join(self.agent, "README.md"), data=b"changed\n")
        self.assertEqual(Git().check_removable(part, force=False),
                         "1 uncommitted change(s); use force to remove anyway")
        self.assertIsNone(Git().check_removable(part, force=True))

    def test_removal_through_the_executor_unregisters_the_worktree_and_keeps_the_main_checkout(self):
        root = self.scan()
        leaf = leaf_owning(root, self.agent)
        results = self.executor().run(build_plan(root, {leaf.id: "delete"}))
        self.assertTrue(results[0].ok, results[0].error)
        self.assertFalse(os.path.exists(self.agent))
        self.assertNotIn(self.agent, self.listed())
        self.assertTrue(os.path.exists(os.path.join(self.repo, "README.md")))

    def test_the_branch_is_deleted_after_removal_only_when_it_is_merged(self):
        unmerged = self.add_worktree("claude/unmerged", os.path.join(self.repo, ".claude", "worktrees", "bar"))
        touch(os.path.join(unmerged, "new.txt"), data=b"work\n")
        git(unmerged, "add", "new.txt")
        git(unmerged, "commit", "-q", "-m", "agent work")
        root = self.scan()
        self.assertIn("unmerged", leaf_owning(root, unmerged).flags)
        marks = {leaf_owning(root, p).id: "delete" for p in (self.agent, unmerged)}

        results = self.executor().run(build_plan(root, marks, delete_branches=True))

        self.assertTrue(all(r.ok for r in results), [r.error for r in results])
        branches = git(self.repo, "branch", "--list", "--format=%(refname:short)").split()
        self.assertNotIn("claude/foo", branches)
        self.assertIn("claude/unmerged", branches)


if __name__ == "__main__":
    unittest.main()
