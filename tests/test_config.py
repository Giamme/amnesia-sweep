from __future__ import annotations

import json
import os
import unittest

from amnesia_sweep import config
from tests.fixtures import SandboxTestCase, write_json


class ConfigTests(SandboxTestCase):
    def test_a_missing_file_gives_the_documented_defaults(self):
        cfg = config.load(self.env.config_dir)
        self.assertEqual((cfg.retention_days, cfg.active_grace_minutes, cfg.worktree_roots), (30, 15, ["~/dev"]))
        self.assertEqual((cfg.include_models, cfg.include_user_content, cfg.all_worktrees), (False, False, False))

    def test_set_value_parses_json_text_into_the_settings_type(self):
        cfg = config.Config()
        self.assertEqual(config.set_value(cfg, "retention-days", "45"), 45)
        config.set_value(cfg, "include_models", "true")
        config.set_value(cfg, "exclude", '["~/keep/*"]')
        self.assertEqual((cfg.retention_days, cfg.include_models, cfg.exclude), (45, True, ["~/keep/*"]))

    def test_set_value_rejects_wrong_types_and_out_of_range_values(self):
        cases = (("retention_days", "abc"), ("retention_days", "1.5"), ("retention_days", "true"),
                 ("retention_days", "0"), ("include_models", "1"), ("exclude", '"one string"'),
                 ("no_such_setting", "1"))
        for key, text in cases:
            with self.subTest(key=key, text=text):
                with self.assertRaises(config.ConfigError):
                    config.set_value(config.Config(), key, text)
        self.assertEqual(config.set_value(config.Config(), "retention_days", "1"), 1)  # the edge is allowed

    def test_unknown_keys_survive_a_load_and_save(self):
        write_json(config.config_path(self.env.config_dir), {"retention_days": 10, "from_the_future": {"a": [1]}})
        cfg = config.load(self.env.config_dir)
        config.save(cfg, self.env.config_dir)
        with open(os.path.join(self.env.config_dir, "config.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual((saved["retention_days"], saved["from_the_future"]), (10, {"a": [1]}))

    def test_a_file_with_a_wrong_type_is_an_error_not_a_silent_default(self):
        cases = ({"retention_days": "thirty"}, {"include_models": "yes"},
                 {"exclude": "~/keep"})  # a string, not a list: it would be matched character by character
        for content in cases:
            with self.subTest(content=content):
                write_json(config.config_path(self.env.config_dir), content)
                with self.assertRaises(config.ConfigError):
                    config.load(self.env.config_dir)


if __name__ == "__main__":
    unittest.main()
