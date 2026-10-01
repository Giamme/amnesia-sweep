from __future__ import annotations

import unittest

from amnesia_sweep.env import iso
from amnesia_sweep.model import Node, Part, finalize
from amnesia_sweep.scan import enforce_invariants, filter_older_than, overlay_archive
from tests.fixtures import by_id

NOW = 1_800_000_000.0
DAY = 86400.0


def leaf(node_id: str, path: str, nbytes: int = 100, newest: float = NOW - 40 * DAY) -> Node:
    return Node(id=node_id, label=node_id, parts=[Part(path=path, bytes=nbytes, files=1, newest=newest)])


def tree(*children: Node) -> Node:
    return finalize(Node(id="", label="All agents", kind="root", children=list(children)))


def group(node_id: str, *children: Node) -> Node:
    return Node(id=node_id, label=node_id, kind="category", children=list(children))


class EnforceInvariantsTests(unittest.TestCase):
    def test_a_leaf_containing_another_listed_path_becomes_inactionable(self):
        root = tree(leaf("outer", "/x/a"), leaf("inner", "/x/a/b"),
                    leaf("name", "/y/a"), leaf("longer-name", "/y/ab"))
        warnings = enforce_invariants(root)
        nodes = by_id(root)
        self.assertFalse(nodes["outer"].actionable)
        self.assertEqual(nodes["outer"].reason, "overlaps another listed item")
        self.assertTrue(nodes["inner"].actionable)
        self.assertTrue(nodes["name"].actionable)  # /y/ab shares a name prefix, not a folder
        self.assertTrue(nodes["longer-name"].actionable)
        self.assertEqual(warnings, ["outer: overlaps inner"])

    def test_two_leaves_owning_the_same_path_leave_only_one_actionable(self):
        root = tree(leaf("first", "/x/a"), leaf("second", "/x/a"))
        enforce_invariants(root)
        nodes = by_id(root)
        self.assertEqual(sorted([nodes["first"].actionable, nodes["second"].actionable]), [False, True])


class OverlayArchiveTests(unittest.TestCase):
    def setUp(self):
        self.root = tree(group("tool",
                               group("tool/s1", leaf("tool/s1/a", "/p/1"), leaf("tool/s1/b", "/p/2")),
                               group("tool/s2", leaf("tool/s2/c", "/p/3"), leaf("tool/s2/d", "/p/4"))))
        self.records = [
            {"id": "tool/s1", "label": "s1", "archived_at": iso(NOW - 5 * DAY), "retention_days": None,
             "parts": [{"path": "/p/1"}, {"path": "/p/2"}]},
            {"id": "tool/s2/c", "label": "c", "archived_at": iso(NOW - 5 * DAY), "retention_days": 7,
             "parts": [{"path": "/p/3"}]},
        ]
        overlay_archive(self.root, self.records, retention_days=30)
        self.nodes = by_id(self.root)

    def info(self, node_id: str):
        archive = self.nodes[node_id].archive
        return None if archive is None else (archive.explicit, archive.partial)

    def test_the_archived_group_is_explicit_and_its_leaves_inherit(self):
        self.assertEqual(self.info("tool/s1"), (True, False))
        self.assertEqual(self.info("tool/s1/a"), (False, False))
        self.assertEqual(self.info("tool/s1/b"), (False, False))

    def test_ancestors_of_some_archived_leaves_are_partially_archived(self):
        self.assertEqual(self.info("tool/s2/c"), (True, False))
        self.assertIsNone(self.info("tool/s2/d"))
        self.assertEqual(self.info("tool/s2"), (False, True))
        self.assertEqual(self.info("tool"), (False, True))
        self.assertIsNone(self.root.archive)

    def test_unpinned_records_follow_the_configured_retention_and_pinned_ones_keep_theirs(self):
        s1, c = self.nodes["tool/s1"].archive, self.nodes["tool/s2/c"].archive
        self.assertEqual((s1.retention_days, s1.pinned, s1.expires_at), (30, False, NOW + 25 * DAY))
        self.assertEqual((c.retention_days, c.pinned, c.expires_at), (7, True, NOW + 2 * DAY))


class FilterOlderThanTests(unittest.TestCase):
    def test_recent_leaves_are_dropped_and_totals_recomputed_on_a_copy(self):
        root = tree(group("tool",
                          group("tool/old", leaf("tool/old/a", "/p/1", 100, NOW - 40 * DAY)),
                          group("tool/mixed", leaf("tool/mixed/b", "/p/2", 30, NOW - 31 * DAY),
                                leaf("tool/mixed/c", "/p/3", 50, NOW - DAY)),
                          group("tool/new", leaf("tool/new/d", "/p/4", 70, NOW - DAY))))

        filtered = filter_older_than(root, 30 * DAY, NOW)

        nodes = by_id(filtered)
        self.assertEqual(set(nodes), {"", "tool", "tool/old", "tool/old/a", "tool/mixed", "tool/mixed/b"})
        self.assertEqual((filtered.bytes, nodes["tool/mixed"].bytes), (130, 30))
        self.assertEqual(root.bytes, 250)  # the original tree is untouched


if __name__ == "__main__":
    unittest.main()
