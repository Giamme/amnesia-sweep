from __future__ import annotations

import fcntl
import io
import json
import os
import time
import unittest

from amnesia_sweep.env import iso
from tests.fixtures import SID, SID2, SandboxTestCase, make_claude, snapshot, touch


def session_files(paths):
    return [p for name, p in paths.items() if name != "memory"]


class CliTests(SandboxTestCase):
    def test_an_unknown_subcommand_exits_2(self):
        code, _, err = self.cli("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_scan_of_an_empty_home_exits_3(self):
        code, _, err = self.cli("scan")
        self.assertEqual(code, 3)
        self.assertIn("no agent data found", err)

    def test_scan_json_is_one_parseable_document_with_the_top_level_keys(self):
        make_claude(self.env)
        code, out, _ = self.cli("scan", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertLessEqual({"version", "totals", "nodes", "expired"}, set(payload))
        self.assertEqual(payload["version"], 1)
        self.assertIn("claude", [n["id"] for n in payload["nodes"]])

    def test_a_broken_pipe_on_stdout_exits_0_quietly(self):
        make_claude(self.env)
        closed = io.StringIO()
        closed.flush = lambda: (_ for _ in ()).throw(BrokenPipeError())
        code, _, err = self.cli("scan", stdout=closed)
        self.assertEqual((code, err), (0, ""))


class CliDeleteTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.paths = make_claude(self.env)
        self.sibling = make_claude(self.env, sid=SID2)

    def assert_all_exist(self, paths):
        for path in paths:
            self.assertTrue(os.path.lexists(path), path)

    def test_delete_yes_removes_the_sessions_files_and_nothing_else(self):
        code, out, _ = self.cli("delete", "--yes", SID)
        self.assertEqual(code, 0, out)
        for path in session_files(self.paths):
            self.assertFalse(os.path.lexists(path), path)
        self.assert_all_exist(session_files(self.sibling) + [self.paths["memory"]])

    def test_an_ambiguous_prefix_exits_2_and_deletes_nothing(self):
        twin = make_claude(self.env, sid=SID[:4] + "9999-2222-3333-4444-555555555555")
        code, _, err = self.cli("delete", "--yes", SID[:4])
        self.assertEqual(code, 2)
        self.assertIn("matches 2 items", err)
        self.assert_all_exist(session_files(self.paths) + session_files(twin))

    def test_delete_without_yes_off_a_terminal_exits_2_and_deletes_nothing(self):
        code, _, err = self.cli("delete", SID, stdin="y\n")
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assert_all_exist(session_files(self.paths))

    def test_delete_dry_run_changes_nothing(self):
        before = snapshot(self.root)
        code, out, _ = self.cli("delete", "--dry-run", SID)
        self.assertEqual(code, 0)
        self.assertIn("Would permanently delete", out)
        self.assertEqual({k: v for k, v in snapshot(self.root).items() if not k.startswith("state")},
                         {k: v for k, v in before.items() if not k.startswith("state")})
        self.assertFalse(os.path.exists(os.path.join(self.env.state_dir, "log.jsonl")))


class CliArchiveTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.paths = make_claude(self.env)

    def archive_json(self) -> bytes:
        with open(os.path.join(self.env.state_dir, "archive.json"), "rb") as handle:
            return handle.read()

    def test_archive_add_then_list_json_shows_the_session(self):
        self.assertEqual(self.cli("archive", "add", SID)[0], 0)
        code, out, _ = self.cli("archive", "list", "--json")
        self.assertEqual(code, 0)
        [record] = json.loads(out)
        self.assertTrue(record["id"].endswith("/" + SID))
        self.assertEqual(set(record["paths"]), set(session_files(self.paths)))
        self.assertEqual((record["retention_days"], record["pinned"]), (30, False))

    def test_sweep_exits_3_while_nothing_archived_has_expired(self):
        self.assertEqual(self.cli("sweep", "--yes")[0], 3)
        self.cli("archive", "add", SID)
        self.assertEqual(self.cli("sweep", "--yes")[0], 3)
        self.assertTrue(os.path.exists(self.paths["transcript"]))

    def test_sweep_yes_deletes_an_archived_session_once_its_retention_is_over(self):
        self.cli("archive", "add", SID)
        code, out, _ = self.cli("sweep", "--yes", AMNESIA_SWEEP_NOW=iso(time.time() + 31 * 86400))
        self.assertEqual(code, 0, out)
        for path in session_files(self.paths):
            self.assertFalse(os.path.lexists(path), path)
        self.assertEqual(json.loads(self.cli("archive", "list", "--json")[1]), [])

    def test_a_corrupt_archive_fails_the_command_and_is_left_byte_for_byte(self):
        corrupt = touch(os.path.join(self.env.state_dir, "archive.json"), data=b'{"version": 1, "rec')
        for argv in (("archive", "add", SID), ("scan",), ("sweep", "--yes")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0], 1)
                self.assertEqual(self.archive_json(), b'{"version": 1, "rec')
        self.assertTrue(os.path.exists(corrupt))

    def test_scan_leaves_the_archive_file_alone_while_another_run_holds_the_lock(self):
        self.cli("archive", "add", SID)
        with open(self.paths["transcript"], "a", encoding="utf-8") as handle:
            handle.write('{"type":"user"}\n')  # the next reconcile will unarchive this part
        before = self.archive_json()
        with open(os.path.join(self.env.state_dir, "lock"), "a+") as other:
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.cli("scan", "--json")[0], 0)
            self.assertEqual(self.archive_json(), before)
            fcntl.flock(other.fileno(), fcntl.LOCK_UN)
        self.assertEqual(self.cli("scan", "--json")[0], 0)
        self.assertNotEqual(self.archive_json(), before)  # positive control: unlocked, it is updated


class CliConfigTests(SandboxTestCase):
    def test_config_set_then_get_round_trips_through_the_file(self):
        code, _, _ = self.cli("config", "set", "retention-days", "45")
        self.assertEqual(code, 0)
        code, out, _ = self.cli("config", "get", "retention_days")
        self.assertEqual((code, out.strip()), (0, "45"))

    def test_config_set_with_a_bad_value_exits_2_and_keeps_the_file(self):
        self.cli("config", "set", "retention-days", "45")
        self.assertEqual(self.cli("config", "set", "retention-days", "0")[0], 2)
        self.assertEqual(self.cli("config", "get", "retention_days")[1].strip(), "45")


if __name__ == "__main__":
    unittest.main()
