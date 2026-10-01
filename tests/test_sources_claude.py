from __future__ import annotations

import os
import time
import unittest

from amnesia_sweep.config import Config
from amnesia_sweep.env import iso
from amnesia_sweep.liveness import Liveness
from tests.fixtures import (OLD, ORPHAN_SID, SID, SID2, FakeRunner, SandboxTestCase, age_tree, by_id,
                            claude_source, leaf_owning, leaves, make_claude, overlaps, paths_of,
                            quiet_liveness, touch, write_json)


def session_node(root, sid):
    return next(n for n in by_id(root).values() if n.kind == "session" and n.meta.get("session_id") == sid)


class ClaudeSessionTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.paths = make_claude(self.env)
        self.source = claude_source()

    def test_one_session_gathers_every_artifact_that_carries_its_id(self):
        session = session_node(self.scan_source(self.source), SID)
        expected = {self.paths[k] for k in ("transcript", "subagents", "file-history", "session-env", "job",
                                            "temp", "cache-break")}
        self.assertEqual(paths_of(session), expected)
        self.assertTrue(all(leaf.actionable for leaf in leaves(session)))
        self.assertEqual(session.meta["title"], "Fix the build")

    def test_the_project_is_labelled_with_the_cwd_its_transcript_records(self):
        root = self.scan_source(self.source)
        project = by_id(root)[session_node(root, SID).id.rsplit("/", 1)[0]]
        self.assertEqual(project.label, "~/dev/proj")
        self.assertNotIn("orphan", project.flags)

    def test_a_project_whose_folder_no_longer_exists_is_flagged_orphan(self):
        make_claude(self.env, sid=SID2, cwd=os.path.join(self.home, "dev", "deleted-proj"))
        root = self.scan_source(self.source)
        project = by_id(root)[session_node(root, SID2).id.rsplit("/", 1)[0]]
        self.assertEqual(project.label, "~/dev/deleted-proj")
        self.assertIn("orphan", project.flags)

    def test_artifacts_without_a_transcript_go_to_the_orphans_category(self):
        history = os.path.dirname(touch(os.path.join(self.env.claude_dir, "file-history", ORPHAN_SID, "f@v1"), 10))
        env_dir = os.path.dirname(touch(os.path.join(self.env.claude_dir, "session-env", ORPHAN_SID, "e"), 10))
        age_tree(self.env.claude_dir)
        root = self.scan_source(self.source)
        orphan = session_node(root, ORPHAN_SID)
        self.assertEqual(orphan.id, f"claude/orphans/{ORPHAN_SID}")
        self.assertEqual(paths_of(orphan), {history, env_dir})
        self.assertTrue(orphan.actionable)

    def test_a_live_session_and_everything_under_it_is_not_actionable(self):
        liveness = quiet_liveness(live_sessions={SID: {"pid": 1, "sessionId": SID, "cwd": "/x"}})
        session = session_node(self.scan_source(self.source, liveness=liveness), SID)
        self.assertFalse(session.actionable)
        for leaf in leaves(session):
            self.assertFalse(leaf.actionable, leaf.id)
            self.assertEqual(leaf.reason, "live session")

    def test_a_session_with_a_running_background_job_is_not_actionable(self):
        write_json(os.path.join(self.paths["job"], "state.json"),
                   {"sessionId": SID, "state": "running", "updatedAt": iso(time.time() - 3600)}, mtime=OLD)
        session = session_node(self.scan_source(self.source), SID)
        self.assertFalse(session.actionable)
        self.assertEqual(session.reason, "background job still active")

    def test_a_session_written_to_minutes_ago_keeps_even_its_old_artifacts(self):
        with open(self.paths["transcript"], "a", encoding="utf-8") as handle:
            handle.write('{"type":"assistant"}\n')
        session = session_node(self.scan_source(self.source), SID)
        file_history = next(l for l in leaves(session) if l.parts[0].path == self.paths["file-history"])
        self.assertFalse(file_history.actionable)
        self.assertEqual(file_history.reason, "active in the last few minutes")

    def test_memory_settings_and_skills_are_never_actionable_nor_inside_an_actionable_leaf(self):
        claude = self.env.claude_dir
        settings = touch(os.path.join(claude, "settings.json"), 10, mtime=OLD)
        skills_target = os.path.dirname(touch(os.path.join(self.outside, "skills", "my-skill", "SKILL.md"), 10))
        skills = os.path.join(claude, "skills")
        os.symlink(os.path.dirname(skills_target), skills)
        root = self.scan_source(self.source, config=Config(include_user_content=True))

        for kept in (self.paths["memory"], settings, skills):
            self.assertFalse(leaf_owning(root, kept).actionable, kept)
        actionable = [p.path for leaf in leaves(root) if leaf.actionable for p in leaf.parts]
        self.assertIn(self.paths["transcript"], actionable)  # positive control
        for path in actionable:
            for kept in (self.paths["memory"], settings, skills, self.outside):
                self.assertFalse(overlaps(path, kept), f"{path} overlaps {kept}")

    def test_unknown_top_level_entries_are_shown_in_other_and_never_actionable(self):
        mystery = os.path.dirname(touch(os.path.join(self.env.claude_dir, "brand-new-feature", "data.bin"), 10))
        age_tree(self.env.claude_dir)
        root = self.scan_source(self.source)
        leaf = leaf_owning(root, mystery)
        self.assertEqual(leaf.id, "claude/other/brand-new-feature")
        self.assertEqual(by_id(root)["claude/other"].label, "Other (not touched)")
        self.assertFalse(leaf.actionable)


class ClaudeBinariesTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.env.claude_dir)
        versions = self.env.path(".local", "share", "claude", "versions")
        self.versions = {v: touch(os.path.join(versions, v), 4096, mtime=OLD) for v in ("2.0.1", "2.0.9", "2.0.10")}
        launcher = self.env.path(".local", "bin", "claude")
        os.makedirs(os.path.dirname(launcher))
        os.symlink(self.versions["2.0.9"], launcher)

    def reasons(self, liveness=None):
        root = self.scan_source(claude_source(), liveness=liveness)
        return {v: (leaf_owning(root, p).actionable, leaf_owning(root, p).reason) for v, p in self.versions.items()}

    def test_the_launcher_target_and_the_newest_version_are_kept_and_older_ones_are_actionable(self):
        self.assertEqual(self.reasons(), {
            "2.0.1": (True, ""),
            "2.0.9": (False, "the version `claude` runs"),
            "2.0.10": (False, "newest version"),   # numeric order, not string order
        })

    def test_an_old_version_that_is_running_or_cant_be_checked_is_kept(self):
        running = Liveness(runner=FakeRunner(lsof=f"p42\nftxt\nn{self.versions['2.0.1']}\n"))
        self.assertEqual(self.reasons(running)["2.0.1"], (False, "a running process uses it"))
        unknown = Liveness(runner=FakeRunner(lsof=None))
        self.assertEqual(self.reasons(unknown)["2.0.1"], (False, "couldn't check whether it's running"))


class ClaudeBackupsTests(SandboxTestCase):
    def test_every_backup_but_the_newest_is_actionable(self):
        backups = os.path.join(self.env.claude_dir, "backups")
        old = [touch(os.path.join(backups, f".claude.json.backup.{n}"), 100, mtime=OLD + n) for n in (1, 2)]
        newest = touch(os.path.join(backups, ".claude.json.backup.0"), 100, mtime=OLD + 50)
        root = self.scan_source(claude_source())
        older = leaf_owning(root, old[0])
        self.assertEqual({p.path for p in older.parts}, set(old))
        self.assertTrue(older.actionable)
        self.assertFalse(leaf_owning(root, newest).actionable)


if __name__ == "__main__":
    unittest.main()
