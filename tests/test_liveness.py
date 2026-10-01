from __future__ import annotations

import os
import unittest

from amnesia_sweep.liveness import Liveness
from tests.fixtures import FakeRunner, SandboxTestCase, dead_pid, touch, write_json

STARTED = "Wed Oct  1 09:00:00 2026"   # ps pads single-digit days with a second space


class ProbeTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.registry = os.path.join(self.env.claude_dir, "sessions")

    def register(self, pid: int, sid: str = "live-session", proc_start: str = "Wed Oct 1 09:00:00 2026") -> None:
        write_json(os.path.join(self.registry, f"{pid}.json"),
                   {"pid": pid, "sessionId": sid, "cwd": "/x", "procStart": proc_start})

    def test_a_registered_running_process_with_a_matching_start_time_is_live(self):
        pid = os.getpid()
        self.register(pid)
        live = Liveness.probe([self.registry], runner=FakeRunner(ps_lstart=f"{pid} {STARTED}\n"))
        self.assertTrue(live.session_live("live-session"))

    def test_a_reused_pid_with_a_different_start_time_is_not_live(self):
        pid = os.getpid()
        self.register(pid)
        live = Liveness.probe([self.registry], runner=FakeRunner(ps_lstart=f"{pid} Thu Oct  2 11:11:11 2026\n"))
        self.assertFalse(live.session_live("live-session"))

    def test_a_dead_pid_is_not_live(self):
        pid = dead_pid()
        self.register(pid)
        live = Liveness.probe([self.registry], runner=FakeRunner(ps_lstart=f"{pid} {STARTED}\n"))
        self.assertFalse(live.session_live("live-session"))

    def test_a_pid_ps_no_longer_lists_is_not_live(self):
        pid = os.getpid()
        self.register(pid)
        live = Liveness.probe([self.registry], runner=FakeRunner(ps_lstart=f"{pid + 1} {STARTED}\n"))
        self.assertFalse(live.session_live("live-session"))

    def test_when_ps_fails_a_running_pid_counts_as_live(self):
        pid = os.getpid()
        self.register(pid)
        for output in (None, ""):
            with self.subTest(ps_output=output):
                live = Liveness.probe([self.registry], runner=FakeRunner(ps_lstart=output))
                self.assertTrue(live.session_live("live-session"))

    def test_process_args_come_from_ps(self):
        live = Liveness.probe([self.registry], runner=FakeRunner(ps_args="  /opt/codex/0.41.0/codex exec\n\n"))
        self.assertTrue(live.arg_mentions("/opt/codex/0.41.0"))
        self.assertFalse(live.arg_mentions("/opt/codex/0.40.0"))


class LsofTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.a = touch(os.path.join(self.root, "a.bin"))
        self.b = touch(os.path.join(self.root, "dir", "b.bin"))
        link = os.path.join(self.root, "via-link")
        os.symlink(os.path.join(self.root, "dir"), link)
        # lsof -F output: p<pid>, f<fd>, n<name> records; names may come through symlinks.
        self.output = f"p123\nfcwd\nn{self.a}\np456\nftxt\nn{link}/b.bin\nf3\nnpipe\n"

    def test_files_open_returns_the_real_paths_lsof_names(self):
        live = Liveness(runner=FakeRunner(lsof=self.output))
        self.assertEqual(live.files_open([self.a, self.b]), {self.a, self.b})

    def test_open_files_returns_the_real_paths_lsof_names(self):
        live = Liveness(runner=FakeRunner(lsof=self.output))
        self.assertEqual(live.open_files("codex"), {self.a, self.b})

    def test_a_failed_lsof_means_unknown_not_nothing_open(self):
        live = Liveness(runner=FakeRunner(lsof=None))
        self.assertIsNone(live.open_files("codex"))
        self.assertIsNone(live.files_open([self.a]))

    def test_a_folder_a_process_works_in_is_in_use(self):
        live = Liveness(runner=FakeRunner(lsof=f"p1\nfcwd\nn{os.path.dirname(self.b)}\n"))
        self.assertTrue(live.path_in_use(os.path.dirname(self.b)))
        self.assertTrue(live.path_in_use(self.root))
        self.assertFalse(live.path_in_use(os.path.join(self.root, "di")))


if __name__ == "__main__":
    unittest.main()
