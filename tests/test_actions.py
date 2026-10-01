from __future__ import annotations

import os
import unittest

from amnesia_sweep.actions import Executor, build_plan, expired_plan, read_log
from amnesia_sweep.archive import ArchiveStore
from amnesia_sweep.env import iso
from amnesia_sweep.model import Node, Part, finalize, make_inactionable
from amnesia_sweep.safety import guard_for
from amnesia_sweep.scan import overlay_archive
from tests.fixtures import (OLD, SID, SID2, SandboxTestCase, age_tree, by_id, claude_source, make_claude,
                            quiet_liveness, snapshot, touch)


def plan_tree() -> Node:
    kept = Node(id="t/s/c", label="c", parts=[Part(path="/p/c", bytes=50)])
    make_inactionable(kept, "protected")
    session = Node(id="t/s", label="s", kind="session", meta={"session_id": "s"}, children=[
        Node(id="t/s/a", label="a", parts=[Part(path="/p/a", bytes=100)]),
        Node(id="t/s/b", label="b", parts=[Part(path="/p/b", bytes=200)]),
        kept])
    return finalize(Node(id="", label="All agents", kind="root",
                         children=[Node(id="t", label="t", kind="tool", children=[session])]))


def ops(plan):
    return [(op.action, op.node_id) for op in plan.ops]


class BuildPlanTests(unittest.TestCase):
    def setUp(self):
        self.root = plan_tree()

    def test_a_group_delete_expands_to_its_actionable_leaves_and_lists_the_rest_with_reasons(self):
        plan = build_plan(self.root, {"t/s": "delete"})
        self.assertEqual(ops(plan), [("delete", "t/s/a"), ("delete", "t/s/b")])
        self.assertEqual([(op.source, op.session_id, op.bytes) for op in plan.ops], [("t", "s", 100), ("t", "s", 200)])
        self.assertEqual([(s.node_id, s.reason) for s in plan.skipped], [("t/s/c", "protected")])

    def test_a_keep_mark_on_a_child_cancels_the_inherited_delete(self):
        plan = build_plan(self.root, {"t/s": "delete", "t/s/b": "keep"})
        self.assertEqual(ops(plan), [("delete", "t/s/a")])

    def test_an_inherited_delete_skips_archived_leaves_but_a_direct_mark_deletes_them(self):
        overlay_archive(self.root, [{"id": "t/s/a", "label": "a", "archived_at": iso(OLD), "retention_days": None,
                                     "parts": [{"path": "/p/a"}]}], 30)
        inherited = build_plan(self.root, {"t/s": "delete"})
        self.assertEqual(ops(inherited), [("delete", "t/s/b")])
        self.assertIn(("t/s/a", "archived (mark it directly to delete it now)"),
                      [(s.node_id, s.reason) for s in inherited.skipped])
        self.assertEqual(ops(build_plan(self.root, {"t/s/a": "delete"})), [("delete", "t/s/a")])

    def test_archiving_a_group_makes_one_record_for_the_group(self):
        plan = build_plan(self.root, {"t/s": "archive"}, retention_days=60)
        self.assertEqual(ops(plan), [("archive", "t/s")])
        self.assertEqual(([p.path for p in plan.ops[0].parts], plan.ops[0].retention_days), (["/p/a", "/p/b"], 60))


class ExecutorTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.paths = make_claude(self.env)
        self.sibling = make_claude(self.env, sid=SID2)
        self.store = ArchiveStore(self.env.state_dir).load()

    def scan(self):
        source = claude_source()
        ctx = self.ctx()
        root = finalize(Node(id="", label="All agents", kind="root", children=[finalize(source.scan(ctx))]))
        return root, guard_for(self.env, [source], ctx)

    def session_id(self, root, sid=SID) -> str:
        return next(n.id for n in by_id(root).values() if n.meta.get("session_id") == sid and n.kind == "session")

    def run_plan(self, marks, dry_run=False, probe_live=None, edit=None):
        root, guard = self.scan()
        marks = {(self.session_id(root) if k == "session" else k): v for k, v in marks.items()}
        plan = build_plan(root, marks)
        if edit:
            edit(plan)
        executor = Executor(self.env, guard, self.store, dry_run=dry_run, probe_live=probe_live)
        return plan, executor.run(plan)

    def session_paths(self, paths):
        return [p for name, p in paths.items() if name != "memory"]

    def test_deleting_a_session_removes_exactly_its_paths(self):
        _, results = self.run_plan({"session": "delete"})
        self.assertTrue(all(r.ok for r in results), [r.error for r in results])
        for path in self.session_paths(self.paths):
            self.assertFalse(os.path.lexists(path), path)
        for path in self.session_paths(self.sibling):
            self.assertTrue(os.path.lexists(path), path)
        self.assertTrue(os.path.exists(os.path.join(self.paths["memory"], "MEMORY.md")))

    def test_every_op_gets_one_audit_log_line(self):
        plan, _ = self.run_plan({"session": "delete"})
        log = read_log(self.env.state_dir)
        self.assertEqual(len(log), len(plan.ops))
        self.assertEqual({p for entry in log for p in entry["paths"]}, set(self.session_paths(self.paths)))
        self.assertEqual({(e["action"], e["result"]) for e in log}, {("delete", "ok")})

    def test_deleting_a_folder_with_a_symlink_to_an_outside_folder_leaves_the_outside_folder_intact(self):
        precious = touch(os.path.join(self.outside, "precious", "keep.txt"), 100)
        os.symlink(os.path.dirname(precious), os.path.join(self.paths["file-history"], "link"))
        age_tree(self.env.claude_dir)
        _, results = self.run_plan({"session": "delete"})
        self.assertTrue(all(r.ok for r in results))
        self.assertFalse(os.path.lexists(self.paths["file-history"]))
        self.assertTrue(os.path.isfile(precious))

    def test_a_dry_run_changes_nothing_and_writes_no_log(self):
        before = snapshot(self.root)
        _, results = self.run_plan({"session": "delete"}, dry_run=True)
        self.assertTrue(results and all(r.ok for r in results))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(os.path.exists(os.path.join(self.env.state_dir, "log.jsonl")))

    def test_a_session_that_went_live_after_the_scan_is_refused_at_delete_time(self):
        live = quiet_liveness(live_sessions={SID: {"pid": 1, "sessionId": SID}})
        _, results = self.run_plan({"session": "delete"}, probe_live=lambda: live)
        self.assertTrue(all(not r.ok and "the session is live now" in r.error for r in results))
        for path in self.session_paths(self.paths):
            self.assertTrue(os.path.lexists(path), path)

    def test_archiving_leaves_every_file_byte_for_byte_and_mtime_for_mtime_untouched(self):
        before = snapshot(self.root)
        plan, results = self.run_plan({"session": "archive"})
        self.assertTrue(results[0].ok)
        outside_state = lambda snap: {k: v for k, v in snap.items() if not k.startswith("state")}  # noqa: E731
        self.assertEqual(outside_state(snapshot(self.root)), outside_state(before))
        self.assertEqual(set(self.store.path_index()), set(self.session_paths(self.paths)))

    @unittest.skipIf(os.geteuid() == 0, "root can unlink inside a read-only folder")
    def test_one_failing_op_does_not_stop_the_rest(self):
        os.chmod(self.env.claude_tmp, 0o555)  # the cache-break file can't be unlinked now
        self.addCleanup(os.chmod, self.env.claude_tmp, 0o755)
        root, _ = self.scan()
        session = self.session_id(root)
        marks = {f"{session}/cache-break-state": "delete", f"{session}/file-history": "delete"}

        plan, results = self.run_plan(marks, edit=lambda p: p.ops.sort(key=lambda op: "cache-break" not in op.node_id))

        self.assertEqual([op.node_id.rsplit("/", 1)[-1] for op in plan.ops], ["cache-break-state", "file-history"])
        self.assertEqual([r.ok for r in results], [False, True])
        self.assertIn("PermissionError", results[0].error)
        self.assertFalse(os.path.lexists(self.paths["file-history"]))
        self.assertEqual([e["result"] for e in read_log(self.env.state_dir)], ["error", "ok"])


class ExpiredPlanTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.paths = make_claude(self.env)
        self.store = ArchiveStore(self.env.state_dir).load()
        self.source = claude_source()
        self.guard = guard_for(self.env, [self.source], self.ctx())

    def archive(self, *names):
        self.store.add("claude/x", "x", "claude", [Part(path=self.paths[n]) for n in names], OLD)

    def sweep(self):
        return Executor(self.env, self.guard, self.store).run(expired_plan(self.store.expired(OLD + 31 * 86400, 30)))

    def append(self, name):
        with open(self.paths[name], "a", encoding="utf-8") as handle:
            handle.write('{"type":"user"}\n')

    def test_an_unchanged_expired_record_is_deleted_and_forgotten(self):
        self.archive("transcript", "file-history")
        results = self.sweep()
        self.assertTrue(results[0].ok, results[0].error)
        self.assertFalse(os.path.lexists(self.paths["transcript"]))
        self.assertFalse(os.path.lexists(self.paths["file-history"]))
        self.assertEqual(self.store.records, [])

    def test_a_part_modified_after_archiving_is_unarchived_not_deleted(self):
        self.archive("transcript")
        self.append("transcript")
        results = self.sweep()
        self.assertTrue(results[0].skipped)
        self.assertIn("changed since it was archived", results[0].error)
        self.assertTrue(os.path.exists(self.paths["transcript"]))
        self.assertEqual(self.store.records, [])

    # Regression: a changed part used to abort the record after earlier parts were already deleted,
    # leaving the archive and the audit log out of step with the disk.
    def test_the_archive_and_the_audit_log_agree_with_the_disk_when_a_later_part_changed(self):
        self.archive("cache-break", "transcript")
        self.append("transcript")
        self.sweep()
        first = self.paths["cache-break"]
        on_disk = os.path.lexists(first)
        self.assertEqual(first in self.store.path_index(), on_disk, "archive.json disagrees with the disk")
        logged = any(first in e["paths"] and e["result"] == "ok" for e in read_log(self.env.state_dir))
        self.assertEqual(logged, not on_disk, "the audit log disagrees with the disk")


if __name__ == "__main__":
    unittest.main()
