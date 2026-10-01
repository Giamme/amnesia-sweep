from __future__ import annotations

import os
import unittest
import urllib.parse

from amnesia_sweep.config import Config
from amnesia_sweep.sources import specs
from tests.fixtures import OLD, SandboxTestCase, age_tree, by_id, leaf_owning, touch


def spec(name: str):
    return next(s for s in specs.sources() if s.name == name)


class SpecSourceTests(SandboxTestCase):
    def test_downloaded_models_need_include_models_to_be_actionable(self):
        model = os.path.dirname(touch(self.env.path(".lmstudio", "models", "qwen", "weights.gguf"), 4096))
        age_tree(self.env.path(".lmstudio"))
        for config, actionable in ((Config(), False), (Config(include_models=True), True)):
            with self.subTest(include_models=config.include_models):
                leaf = leaf_owning(self.scan_source(spec("lmstudio"), config=config), model)
                self.assertEqual(leaf.actionable, actionable)
                if not actionable:
                    self.assertEqual(leaf.reason, "needs --include-models")

    def test_url_encoded_session_folders_become_readable_group_labels(self):
        folder = urllib.parse.quote(os.path.join(self.home, "dev", "proj"), safe="")
        session = touch(self.env.path(".grok", "sessions", folder, "session-1.json"), 100)
        age_tree(self.env.path(".grok"))
        root = self.scan_source(spec("grok"))
        leaf = leaf_owning(root, session)
        self.assertTrue(leaf.actionable)
        self.assertEqual(by_id(root)[leaf.id.rsplit("/", 1)[0]].label, "~/dev/proj")

    def test_entries_no_rule_matches_land_in_other_and_are_never_actionable(self):
        logs = touch(self.env.path(".lmstudio", "server-logs", "a.log"), 10)
        settings = touch(self.env.path(".lmstudio", "settings.json"), 10)
        unknown = touch(self.env.path(".lmstudio", "mystery.db"), 10)
        age_tree(self.env.path(".lmstudio"), OLD)
        root = self.scan_source(spec("lmstudio"))
        self.assertTrue(leaf_owning(root, os.path.dirname(logs)).actionable)  # positive control
        for path in (settings, unknown):
            leaf = leaf_owning(root, path)
            self.assertTrue(leaf.id.startswith("lmstudio/other/"), leaf.id)
            self.assertFalse(leaf.actionable)


if __name__ == "__main__":
    unittest.main()
