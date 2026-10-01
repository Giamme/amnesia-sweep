from __future__ import annotations

import fcntl
import json
import os
import unittest

from amnesia_sweep.archive import ArchiveError, ArchiveStore, LockHeld, atomic_write, locked
from amnesia_sweep.env import iso, parse_when
from amnesia_sweep.model import Part
from tests.fixtures import OLD, SandboxTestCase, age_tree, touch

DAY = 86400.0
T0 = 1_800_000_000.0


class StoreTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.state = self.env.state_dir
        self.folder = os.path.dirname(touch(os.path.join(self.root, "data", "session", "a.txt"), 3000))
        self.file = touch(os.path.join(self.root, "data", "transcript.jsonl"), 5000)
        age_tree(os.path.join(self.root, "data"), OLD)

    def store_with(self, *paths: str, record_id: str = "claude/s1", retention_days=None) -> ArchiveStore:
        store = ArchiveStore(self.state).load()
        store.add(record_id, "s1", "claude", [Part(path=p) for p in paths], T0, retention_days)
        return store

    def test_records_survive_a_save_and_load_round_trip(self):
        self.store_with(self.folder, self.file, retention_days=45).save()
        record = ArchiveStore(self.state).load().get("claude/s1")
        self.assertEqual((record["label"], record["source"], record["retention_days"]), ("s1", "claude", 45))
        self.assertEqual(parse_when(record["archived_at"]), T0)
        self.assertEqual([p["path"] for p in record["parts"]], [self.folder, self.file])
        self.assertEqual(record["parts"][1]["files"], 1)
        self.assertGreater(record["parts"][1]["bytes"], 0)

    def test_saving_leaves_only_the_archive_file_behind(self):
        self.store_with(self.file).save()
        self.assertEqual(os.listdir(self.state), ["archive.json"])

    def test_a_failed_atomic_write_keeps_the_old_file_and_no_temp_file(self):
        path = os.path.join(self.state, "archive.json")
        atomic_write(path, '{"version": 1, "records": []}\n')
        with self.assertRaises(UnicodeEncodeError):
            atomic_write(path, "half written \udc80")
        self.assertEqual(os.listdir(self.state), ["archive.json"])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"version": 1, "records": []}\n')

    def test_an_unreadable_archive_raises_instead_of_being_replaced(self):
        cases = (("not json", "{not json"), ("not an archive", "[1, 2]"),
                 ("from a newer version", json.dumps({"version": 99, "records": []})))
        path = os.path.join(self.state, "archive.json")
        for name, text in cases:
            with self.subTest(name):
                touch(path, data=text.encode())
                with self.assertRaises(ArchiveError):
                    ArchiveStore(self.state).load()

    def test_reconcile_unarchives_only_the_part_that_changed(self):
        store = self.store_with(self.folder, self.file)
        with open(self.file, "ab") as handle:
            handle.write(b"more" * 4096)

        events = store.reconcile()

        self.assertEqual([(e.kind, e.path) for e in events], [("unarchived", self.file)])
        self.assertEqual([p["path"] for p in store.get("claude/s1")["parts"]], [self.folder])

    def test_reconcile_drops_vanished_paths_and_then_records_left_with_no_parts(self):
        store = self.store_with(self.file)
        store.add("claude/s2", "s2", "claude", [Part(path=self.folder)], T0)
        os.unlink(self.file)

        events = store.reconcile()

        self.assertEqual([(e.kind, e.record_id) for e in events], [("gone", "claude/s1")])
        self.assertIsNone(store.get("claude/s1"))
        self.assertIsNotNone(store.get("claude/s2"))

    def test_records_expire_after_the_default_retention_unless_pinned(self):
        store = self.store_with(self.file, record_id="follows-default")
        store.add("pinned", "p", "claude", [Part(path=self.folder)], T0, retention_days=30)
        expired = lambda now, days: [r["id"] for r in store.expired(now, days)]  # noqa: E731
        self.assertEqual(expired(T0 + 7 * DAY - 1, 7), [])
        self.assertEqual(expired(T0 + 7 * DAY, 7), ["follows-default"])
        self.assertEqual(expired(T0 + 7 * DAY, 60), [])
        self.assertEqual(expired(T0 + 30 * DAY, 60), ["pinned"])

    def test_keep_restarts_the_retention_period_and_keeps_a_pinned_retention(self):
        store = self.store_with(self.file, retention_days=10)
        store.keep("claude/s1", T0 + 20 * DAY)
        record = store.get("claude/s1")
        self.assertEqual(record["archived_at"], iso(T0 + 20 * DAY))
        self.assertEqual(record["retention_days"], 10)
        self.assertEqual(store.expired(T0 + 25 * DAY, 30), [])


class LockTests(SandboxTestCase):
    def test_a_second_run_cannot_take_the_lock_while_it_is_held(self):
        os.makedirs(self.env.state_dir)
        with open(os.path.join(self.env.state_dir, "lock"), "a+") as other:
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(LockHeld):
                with locked(self.env.state_dir):
                    pass
            fcntl.flock(other.fileno(), fcntl.LOCK_UN)
        with locked(self.env.state_dir):
            pass  # free again once the other holder lets go


if __name__ == "__main__":
    unittest.main()
