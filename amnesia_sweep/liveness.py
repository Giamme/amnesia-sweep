"""What is in use right now: live agent sessions, running binaries, and process working dirs.

Anything in use is shown but never offered for removal. When a check can't be completed, the
answer is "in use": a wrong "live" only hides an item, a wrong "dead" could delete a running
session's files. Standard library only.
"""

from __future__ import annotations

import glob
import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

from .transcripts import read_json

Runner = Callable[[List[str]], Optional[str]]


def run(argv: List[str], timeout: float = 20.0) -> Optional[str]:
    """Run a command and return its stdout, or None if it failed in any way."""
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, env=env, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode not in (0, 1):
        return None
    return done.stdout.decode("utf-8", "replace")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass
class Liveness:
    live_sessions: Dict[str, dict] = field(default_factory=dict)    # sessionId -> registry record
    process_args: List[str] = field(default_factory=list)
    runner: Runner = run
    _cwds: Optional[Set[str]] = None
    _open: Dict[str, Optional[Set[str]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def probe(cls, registry_dirs: Iterable[str], runner: Runner = run) -> "Liveness":
        live = cls(runner=runner)
        records = []
        for directory in registry_dirs:
            for path in glob.glob(os.path.join(glob.escape(directory), "*.json")):
                record = read_json(path)
                pid = record.get("pid")
                sid = record.get("sessionId")
                if isinstance(pid, int) and isinstance(sid, str) and pid_alive(pid):
                    records.append(record)
        starts = live._start_times([r["pid"] for r in records])
        for record in records:
            stored = record.get("procStart")
            actual = starts.get(record["pid"]) if starts is not None else None
            # Only a successful ps that disagrees with the stored start time proves the pid was
            # reused by some other process. Anything else counts as live.
            if starts is not None and isinstance(stored, str) and actual is not None \
                    and _norm(actual) != _norm(stored):
                continue
            if starts is not None and actual is None and isinstance(stored, str):
                continue  # ps ran fine and the pid is gone
            live.live_sessions[record["sessionId"]] = record
        args = runner(["ps", "-axo", "args="])
        live.process_args = [line.strip() for line in (args or "").splitlines() if line.strip()]
        return live

    def _start_times(self, pids: List[int]) -> Optional[Dict[int, str]]:
        if not pids:
            return {}
        out = self.runner(["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)])
        if not out or not out.strip():
            # These pids answered kill(0) a moment ago; an empty listing means ps failed.
            return None
        starts: Dict[int, str] = {}
        for line in out.splitlines():
            bits = line.strip().split(None, 1)
            if len(bits) == 2 and bits[0].isdigit():
                starts[int(bits[0])] = bits[1]
        return starts

    def session_live(self, sid: str) -> bool:
        return sid in self.live_sessions

    def live_versions(self) -> Set[str]:
        return {str(r["version"]) for r in self.live_sessions.values() if r.get("version")}

    def arg_mentions(self, path: str) -> bool:
        return any(path in args for args in self.process_args)

    def process_cwds(self) -> Set[str]:
        """Working directories of every process we can see (one lsof call, cached)."""
        with self._lock:
            if self._cwds is None:
                out = self.runner(["lsof", "-a", "-d", "cwd", "-Fn", "-w"])
                cwds = set()
                for line in (out or "").splitlines():
                    if line.startswith("n/"):
                        cwds.add(os.path.realpath(line[1:]))
                self._cwds = cwds
            return self._cwds

    def open_files(self, command: str) -> Optional[Set[str]]:
        """Files held open by processes whose name starts with `command` (one lsof call, cached).

        None means lsof couldn't answer, so callers must assume recent files are open.
        """
        with self._lock:
            if command not in self._open:
                out = self.runner(["lsof", "-w", "-Fn", "-c", command])
                self._open[command] = None if out is None else {
                    os.path.realpath(line[1:]) for line in out.splitlines() if line.startswith("n/")}
            return self._open[command]

    def files_open(self, paths: List[str]) -> Optional[Set[str]]:
        """Which of these files some process has open or is running (one lsof call).

        None means lsof couldn't answer.
        """
        if not paths:
            return set()
        out = self.runner(["lsof", "-w", "-Fn", "--", *paths])
        if out is None:
            return None
        return {os.path.realpath(line[1:]) for line in out.splitlines() if line.startswith("n/")}

    def path_in_use(self, path: str) -> bool:
        real = os.path.realpath(path)
        prefix = real.rstrip(os.sep) + os.sep
        return any(cwd == real or cwd.startswith(prefix) for cwd in self.process_cwds())
