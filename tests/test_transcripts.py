from __future__ import annotations

import json
import os
import unittest

from amnesia_sweep.transcripts import claude_head, codex_meta, enc
from tests.fixtures import SandboxTestCase, touch

TRICKY_CWD = '/Users/a/dir with "quotes" and café ☃'


def jsonl(*records) -> bytes:
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records).encode()


class ClaudeHeadTests(SandboxTestCase):
    def write(self, *records) -> str:
        return touch(os.path.join(self.root, "t.jsonl"), data=jsonl(*records))

    def test_cwd_on_line_five_is_found_and_unescaped(self):
        path = self.write(*({"type": "summary", "n": i} for i in range(4)),
                          {"type": "user", "cwd": TRICKY_CWD, "message": {"content": "hi"}})
        self.assertEqual(claude_head(path)["cwd"], TRICKY_CWD)

    def test_cwd_beyond_max_lines_is_not_read(self):
        path = self.write(*({"type": "summary", "n": i} for i in range(4)), {"type": "user", "cwd": "/x/y"})
        self.assertNotIn("cwd", claude_head(path, max_lines=4))
        self.assertEqual(claude_head(path, max_lines=5)["cwd"], "/x/y")

    def test_custom_title_and_agent_name_become_the_title(self):
        cases = (("custom-title", {"type": "custom-title", "customTitle": "Fix the build"}, "Fix the build"),
                 ("agent-name", {"type": "agent-name", "agentName": "reviewer"}, "reviewer"))
        for name, record, expected in cases:
            with self.subTest(name):
                path = self.write({"type": "user", "cwd": "/x"}, record)
                self.assertEqual(claude_head(path), {"cwd": "/x", "title": expected})

    def test_a_missing_file_gives_nothing(self):
        self.assertEqual(claude_head(os.path.join(self.root, "nope.jsonl")), {})


class CodexMetaTests(SandboxTestCase):
    def test_session_meta_on_line_one_gives_cwd_and_id(self):
        sid = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
        path = touch(os.path.join(self.root, "rollout.jsonl"), data=jsonl(
            {"timestamp": "2026-08-01T10:00:00Z", "type": "session_meta",
             "payload": {"id": sid, "timestamp": "2026-08-01T10:00:00Z", "cwd": TRICKY_CWD}},
            {"type": "response_item", "payload": {"id": "ffffffff-0000", "cwd": "/elsewhere"}}))
        self.assertEqual(codex_meta(path), {"cwd": TRICKY_CWD, "id": sid})


class EncTests(unittest.TestCase):
    def test_every_non_alphanumeric_character_becomes_a_dash(self):
        self.assertEqual(enc("/Users/a.b/x_y"), "-Users-a-b-x-y")


if __name__ == "__main__":
    unittest.main()
