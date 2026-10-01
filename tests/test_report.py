from __future__ import annotations

import unittest

from amnesia_sweep.model import Node, Part, finalize
from amnesia_sweep.report import human_age, human_size, node_json, parse_age, parse_size, render_text

NOW = 1_800_000_000.0
DAY = 86400


class FormatTests(unittest.TestCase):
    def test_human_size_uses_1024_based_units(self):
        cases = ((0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (1536, "1.5 KiB"),
                 (5 * 1024 ** 3, "5.0 GiB"), (3 * 1024 ** 5, "3072.0 TiB"))
        for value, text in cases:
            self.assertEqual(human_size(value), text, value)

    def test_human_age_picks_the_largest_sensible_unit(self):
        cases = ((-5, "0m"), (59, "0m"), (3600, "1h"), (3 * DAY, "3d"), (59 * DAY, "59d"),
                 (90 * DAY, "3mo"), (730 * DAY, "2.0y"))
        for seconds, text in cases:
            self.assertEqual(human_age(seconds), text, seconds)

    def test_parse_size_reads_binary_units(self):
        cases = (("1.5G", 1610612736), ("500M", 524288000), ("10k", 10240), ("2GiB", 2147483648), ("0", 0))
        for text, value in cases:
            self.assertEqual(parse_size(text), value, text)
        with self.assertRaises(ValueError):
            parse_size("lots")

    def test_parse_age_reads_units_and_defaults_to_days(self):
        cases = (("2w", 14 * DAY), ("6h", 6 * 3600), ("30", 30 * DAY), ("5m", 300), ("2mo", 60 * DAY))
        for text, value in cases:
            self.assertEqual(parse_age(text), value, text)
        with self.assertRaises(ValueError):
            parse_age("soon")


def sample_tree() -> Node:
    def leaf(node_id, nbytes):
        return Node(id=node_id, label=node_id.rsplit("/", 1)[-1],
                    parts=[Part(path="/p/" + node_id, bytes=nbytes, files=1, newest=NOW - 3 * DAY)])

    tool = Node(id="tool", label="Some Tool", kind="tool", children=[
        Node(id="tool/big", label="big-project", kind="project", children=[leaf("tool/big/leaf-a", 5 * 1024 ** 2)]),
        Node(id="tool/small", label="small-project", kind="project", children=[leaf("tool/small/leaf-b", 10)])])
    return finalize(Node(id="", label="All agents", kind="root", children=[tool]))


class RenderTextTests(unittest.TestCase):
    def test_depth_limits_how_many_levels_are_shown(self):
        root = sample_tree()
        shallow = render_text(root, NOW, depth=1, top=0)
        self.assertIn("Some Tool", shallow)
        self.assertNotIn("big-project", shallow)
        deeper = render_text(root, NOW, depth=2, top=0)
        self.assertIn("big-project", deeper)
        self.assertNotIn("leaf-a", deeper)

    def test_min_size_hides_small_rows_but_never_a_tool(self):
        text = render_text(sample_tree(), NOW, depth=3, min_size=1024 ** 2, top=0)
        self.assertIn("big-project", text)
        self.assertNotIn("small-project", text)
        tiny_tool = render_text(sample_tree(), NOW, depth=1, min_size=1024 ** 4, top=0)
        self.assertIn("Some Tool", tiny_tool)


class NodeJsonTests(unittest.TestCase):
    def test_node_json_has_the_documented_shape_and_respects_depth(self):
        data = node_json(sample_tree().children[0], NOW, depth=1)
        self.assertEqual(set(data), {"id", "label", "kind", "risk", "bytes", "files", "newest", "reclaimable",
                                     "actionable", "reason", "flags", "meta", "paths", "archive", "children"})
        self.assertEqual([c["id"] for c in data["children"]], ["tool/big", "tool/small"])  # biggest first
        self.assertEqual(data["children"][0]["children"], [])
        leaf = node_json(sample_tree().children[0].children[0].children[0], NOW)
        self.assertEqual((leaf["paths"], leaf["archive"], leaf["reclaimable"]),
                         (["/p/tool/big/leaf-a"], None, 5 * 1024 ** 2))


if __name__ == "__main__":
    unittest.main()
