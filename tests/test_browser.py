from __future__ import annotations

import unittest

from amnesia_sweep.actions import Result
from amnesia_sweep.browser import BrowserState, Effect, add_source, handle_key, remove_leaves, render
from amnesia_sweep.env import iso
from amnesia_sweep.model import Node, Part, finalize, make_inactionable

NOW = 1_800_000_000.0
DAY = 86400.0


def leaf(node_id: str, nbytes: int, actionable: bool = True, reason: str = "", flags=()) -> Node:
    node = Node(id=node_id, label=node_id.rsplit("/", 1)[-1], kind="artifact", flags=set(flags),
                parts=[Part(path="/p/" + node_id, bytes=nbytes, files=1, newest=NOW - 40 * DAY)])
    if not actionable:
        make_inactionable(node, reason)
    return node


def sample_root() -> Node:
    """claude (caches 1000 B, sessions: s1 = a 300 + b 200, memory 50 kept) and codex (100 B)."""
    session = Node(id="claude/sessions/s1", label="s1  Fix the build", kind="session", meta={"session_id": "s1"},
                   children=[leaf("claude/sessions/s1/a", 300), leaf("claude/sessions/s1/b", 200)])
    sessions = Node(id="claude/sessions", label="Sessions", kind="category", children=[
        session, leaf("claude/sessions/memory", 50, actionable=False, reason="project memory is never touched")])
    caches = Node(id="claude/caches", label="Caches & logs", kind="category",
                  children=[leaf("claude/caches/debug", 1000, flags={"recent"})])
    claude = Node(id="claude", label="Claude Code", kind="tool", children=[caches, sessions])
    codex = Node(id="codex", label="Codex", kind="tool", children=[leaf("codex/old", 100)])
    return finalize(Node(id="", label="All agents", kind="root", children=[claude, codex]))


def press(state: BrowserState, *keys: str):
    effect = None
    for key in keys:
        effect = handle_key(state, key)
    return effect


class BrowserTestCase(unittest.TestCase):
    def setUp(self):
        self.state = BrowserState(root=sample_root(), now=NOW, retention_days=30, order=["claude", "codex"])

    def selected(self) -> str:
        return self.state.selected().id

    def plan_ids(self):
        return [op.node_id for op in self.state.plan.ops]


class NavigationTests(BrowserTestCase):
    def test_right_opens_a_folder_and_left_returns_with_the_cursor_on_it(self):
        press(self.state, "right", "down", "right")
        self.assertEqual(self.state.current.id, "claude/sessions")
        self.assertEqual(self.selected(), "claude/sessions/s1")  # biggest first
        press(self.state, "left")
        self.assertEqual(self.state.current.id, "claude")
        self.assertEqual(self.selected(), "claude/sessions")
        press(self.state, "left", "down")
        self.assertEqual(self.selected(), "codex")


class MarkTests(BrowserTestCase):
    def test_a_delete_mark_on_a_group_covers_its_actionable_leaves(self):
        press(self.state, "right", "down", "right", "d")
        self.assertEqual(self.state.marks, {"claude/sessions/s1": "delete"})
        self.assertEqual(self.state.marked_totals(), {"delete": (2, 500)})

    def test_unmarking_a_child_of_a_marked_group_keeps_just_that_child(self):
        press(self.state, "right", "down", "right", "d", "up", "right", "u")
        self.assertEqual(self.state.marks["claude/sessions/s1/a"], "keep")
        press(self.state, "x")
        self.assertEqual(self.plan_ids(), ["claude/sessions/s1/b"])

    def test_marking_something_that_cannot_be_removed_is_refused_with_its_reason(self):
        press(self.state, "right", "down", "right", "down")
        self.assertEqual(self.selected(), "claude/sessions/memory")
        press(self.state, "d")
        self.assertEqual(self.state.marks, {})
        self.assertEqual(self.state.message, "can't delete this: project memory is never touched")


class ConfirmAndQuitTests(BrowserTestCase):
    def test_x_builds_a_plan_whose_skipped_items_carry_their_reasons(self):
        press(self.state, "right", "down", "d", "x")
        self.assertEqual(self.state.screen, "confirm")
        self.assertEqual(self.plan_ids(), ["claude/sessions/s1/a", "claude/sessions/s1/b"])
        self.assertEqual([(s.node_id, s.reason) for s in self.state.plan.skipped],
                         [("claude/sessions/memory", "project memory is never touched")])

    def test_y_on_the_confirm_screen_applies_the_reviewed_plan(self):
        press(self.state, "right", "down", "d", "x")
        effect = press(self.state, "y")
        self.assertEqual(effect.kind, "apply")
        self.assertIs(effect.plan, self.state.plan)

    def test_quitting_with_marks_asks_first(self):
        press(self.state, "d")
        self.assertIsNone(press(self.state, "q"))
        self.assertEqual(self.state.screen, "quit")
        self.assertIsNone(press(self.state, "n"))
        self.assertEqual((self.state.screen, self.state.marks), ("browse", {"claude": "delete"}))
        press(self.state, "q")
        self.assertEqual(press(self.state, "y"), Effect("quit"))

    def test_quitting_without_marks_quits_at_once(self):
        self.assertEqual(press(self.state, "q"), Effect("quit"))


class ExpiryTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        self.state.expired = [
            {"id": f"claude/sessions/s{n}", "label": f"session {n}", "archived_at": iso(NOW - 40 * DAY),
             "retention_days": None, "parts": [{"path": f"/p/{n}", "bytes": 100 * n}]} for n in (1, 2)]
        self.state.screen = "expiry"

    def test_checking_an_item_and_confirming_expires_just_that_item(self):
        self.assertIsNone(press(self.state, "down", "space", "enter"))
        self.assertEqual(press(self.state, "y"), Effect("expire", ids=["claude/sessions/s2"]))

    # Regression: "k" used to move the cursor up on this screen, so keep was unreachable.
    def test_k_on_the_expiry_screen_keeps_every_item_for_another_period(self):
        self.assertEqual(press(self.state, "k"), Effect("keep", ids=["claude/sessions/s1", "claude/sessions/s2"]))


class TreeUpdateTests(BrowserTestCase):
    def test_add_source_keeps_the_cursor_on_the_same_node(self):
        press(self.state, "down")
        self.assertEqual(self.selected(), "codex")
        add_source(self.state, finalize(Node(id="gemini", label="Gemini", kind="tool",
                                             children=[leaf("gemini/tmp", 5000)])))
        self.assertEqual([n.id for n in self.state.visible()], ["gemini", "claude", "codex"])
        self.assertEqual(self.selected(), "codex")

    def test_a_source_that_found_nothing_gets_no_row(self):
        add_source(self.state, finalize(Node(id="gemini", label="Gemini", kind="tool")))
        self.assertEqual([n.id for n in self.state.visible()], ["claude", "codex"])

    def test_remove_leaves_drops_emptied_groups_and_re_aggregates_totals(self):
        remove_leaves(self.state, {"claude/sessions/s1/a", "claude/sessions/s1/b"})
        self.assertIsNone(self.state.node("claude/sessions/s1"))
        self.assertEqual((self.state.node("claude/sessions").bytes, self.state.node("claude").bytes,
                          self.state.root.bytes), (50, 1050, 1150))


class RenderTests(BrowserTestCase):
    def screens(self):
        """(name, state set up for that screen)."""
        yield "browse at the top", self.state
        browse = BrowserState(root=sample_root(), now=NOW, retention_days=30)
        press(browse, "right", "down", "right", "d")
        yield "browse inside a marked folder", browse
        confirm = BrowserState(root=sample_root(), now=NOW, retention_days=30)
        press(confirm, "right", "down", "d", "x")
        yield "confirm", confirm
        for name, keys in (("help", ("?",)), ("info", ("i",)), ("quit", ("d", "q"))):
            state = BrowserState(root=sample_root(), now=NOW, retention_days=30)
            press(state, *keys)
            yield name, state
        expiry = BrowserState(root=sample_root(), now=NOW, retention_days=30, screen="expiry", expired=[
            {"id": "x", "label": "a very long archived label " * 5, "archived_at": iso(NOW - 40 * DAY),
             "retention_days": None, "parts": [{"path": "/p", "bytes": 1}]}])
        yield "expiry", expiry
        result = BrowserState(root=sample_root(), now=NOW, retention_days=30, screen="result")
        result.results = [Result(op=confirm.plan.ops[0], ok=False, error="PermissionError: " + "x" * 200)] * 30
        yield "result with more failures than rows", result

    def test_every_screen_fills_exactly_the_terminal_and_never_overflows_a_row(self):
        for name, state in self.screens():
            with self.subTest(name):
                rows = render(state, 80, 24)
                self.assertEqual(len(rows), 24)
                for row in rows:
                    self.assertLessEqual(len("".join(text for text, _ in row)), 80, row)

    def test_a_terminal_below_the_minimum_size_says_so(self):
        rows = render(self.state, 40, 8)
        text = "".join(t for t, _ in rows[0])
        self.assertIn("terminal too small", text)
        self.assertLessEqual(len(text), 40)


if __name__ == "__main__":
    unittest.main()
