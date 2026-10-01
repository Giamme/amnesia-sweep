"""Shared sandbox and fixture builders for the amnesia-sweep tests.

Every test runs against a throwaway directory: a fake HOME, fake /tmp and $TMPDIR, and fake
state/config dirs, with ps/lsof answered by a canned runner. Nothing here may touch the real
home directory, ~/.claude, ~/.codex or /tmp/claude-<uid>.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Dict, Iterable, List, Optional
from unittest import mock

from amnesia_sweep.config import Config
from amnesia_sweep.env import Env
from amnesia_sweep.liveness import Liveness
from amnesia_sweep.model import Node, finalize, iter_leaves, iter_nodes
from amnesia_sweep.sources.base import ScanContext
from amnesia_sweep.transcripts import enc

DAY = 86400.0
OLD = time.time() - 40 * DAY          # well past every recency and min-age rule

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "66666666-7777-8888-9999-aaaaaaaaaaaa"
ORPHAN_SID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def touch(path: str, size: int = 0, mtime: Optional[float] = None, data: Optional[bytes] = None) -> str:
    """Create a file (and its parents) holding `data`, or `size` bytes of x, at the given mtime."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if data is not None else b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_json(path: str, value, mtime: Optional[float] = None) -> str:
    # Compact separators: Claude Code and Codex write JSON without spaces.
    return touch(path, data=(json.dumps(value, separators=(",", ":")) + "\n").encode(), mtime=mtime)


def age_tree(path: str, mtime: float = OLD) -> None:
    """Set mtime on a path and everything under it, never following symlinks."""
    if os.path.isdir(path) and not os.path.islink(path):
        for dirpath, dirnames, filenames in os.walk(path, topdown=False):
            for name in filenames + dirnames:
                os.utime(os.path.join(dirpath, name), (mtime, mtime), follow_symlinks=False)
    os.utime(path, (mtime, mtime), follow_symlinks=False)


def snapshot(path: str) -> Dict[str, tuple]:
    """{relative path: (bytes or link target, mtime_ns)} for everything under path."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            if os.path.islink(full):
                content = "->" + os.readlink(full)
            elif os.path.isdir(full):
                content = "dir"
            else:
                with open(full, "rb") as handle:
                    content = handle.read()
            found[os.path.relpath(full, path)] = (content, st.st_mtime_ns)
    return found


class FakeRunner:
    """Stands in for liveness.run: canned stdout per command, every call recorded."""

    def __init__(self, ps_lstart: Optional[str] = "", ps_args: Optional[str] = "",
                 lsof: Optional[str] = ""):
        self.ps_lstart = ps_lstart
        self.ps_args = ps_args
        self.lsof = lsof
        self.calls: List[List[str]] = []

    def __call__(self, argv: List[str]) -> Optional[str]:
        self.calls.append(list(argv))
        if argv[:2] == ["ps", "-o"]:
            return self.ps_lstart
        if argv[:2] == ["ps", "-axo"]:
            return self.ps_args
        if argv[0] == "lsof":
            return self.lsof
        return None


def dead_pid() -> int:
    """The pid of a child that has already exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def quiet_liveness(**fields) -> Liveness:
    """Nothing live, nothing open: ps and lsof succeed and list nothing."""
    return Liveness(runner=FakeRunner(), **fields)


# -- tree helpers ---------------------------------------------------------------------------------


def leaves(node: Node) -> List[Node]:
    return list(iter_leaves(node))


def by_id(root: Node) -> Dict[str, Node]:
    return {n.id: n for n in iter_nodes(root)}


def leaf_owning(root: Node, path: str) -> Optional[Node]:
    for leaf in iter_leaves(root):
        if any(p.path == path for p in leaf.parts):
            return leaf
    return None


def paths_of(node: Node) -> set:
    return {p.path for leaf in iter_leaves(node) for p in leaf.parts}


def overlaps(a: str, b: str) -> bool:
    """True when one path equals or contains the other."""
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)


# -- the sandbox ----------------------------------------------------------------------------------


class SandboxTestCase(unittest.TestCase):
    """A fake machine in a temp dir: self.env points every root inside self.root."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="amnesia-sweep-test-")
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home = os.path.join(self.root, "home")
        self.outside = os.path.join(self.root, "outside")  # never a root of any source
        for path in (self.home, self.outside, os.path.join(self.root, "tmp"), os.path.join(self.root, "T")):
            os.makedirs(path)
        self.env = Env(home=self.home, uid=os.getuid(), tmp_root=os.path.join(self.root, "tmp"),
                       tmpdir=os.path.join(self.root, "T"),
                       state_dir=os.path.join(self.root, "state"), config_dir=os.path.join(self.root, "config"),
                       claude_dir=os.path.join(self.home, ".claude"), codex_dir=os.path.join(self.home, ".codex"))
        real_home = os.path.realpath(os.path.expanduser("~"))
        for path in (self.env.home, self.env.tmp_root, self.env.tmpdir, self.env.state_dir,
                     self.env.config_dir, self.env.claude_dir, self.env.codex_dir):
            self.assertTrue(path.startswith(self.root + os.sep), path)
            self.assertNotEqual(path, real_home)

    def ctx(self, config: Optional[Config] = None, liveness: Optional[Liveness] = None) -> ScanContext:
        return ScanContext(env=self.env, config=config or Config(), liveness=liveness or quiet_liveness())

    def scan_source(self, source, config: Optional[Config] = None, liveness: Optional[Liveness] = None) -> Node:
        return finalize(source.scan(self.ctx(config, liveness)))

    # -- CLI ----------------------------------------------------------------------------------------

    def cli_environ(self, **extra: str) -> Dict[str, str]:
        environ = {k: v for k, v in os.environ.items()
                   if not k.startswith(("AMNESIA_SWEEP_", "XDG_", "CLAUDE_", "CODEX_"))}
        environ.update(HOME=self.home, AMNESIA_SWEEP_HOME=self.home, AMNESIA_SWEEP_TMP=self.env.tmp_root,
                       TMPDIR=self.env.tmpdir, AMNESIA_SWEEP_STATE_DIR=self.env.state_dir,
                       AMNESIA_SWEEP_CONFIG_DIR=self.env.config_dir, CLAUDE_CONFIG_DIR=self.env.claude_dir,
                       CODEX_HOME=self.env.codex_dir, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        environ.update(extra)
        return environ

    def cli(self, *argv: str, stdin: str = "", runner: Optional[FakeRunner] = None,
            stdout: Optional[io.StringIO] = None, **environ: str):
        """Run cli.main in the sandbox; returns (exit code, stdout, stderr)."""
        from amnesia_sweep import cli

        runner = runner or FakeRunner()
        real_probe = Liveness.probe.__func__

        def probe(dirs, *args, **kwargs):
            for directory in dirs:
                self.assertTrue(os.path.realpath(directory).startswith(self.root + os.sep), directory)
            return real_probe(Liveness, dirs, runner=runner)

        out, err = stdout or io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.cli_environ(**environ), clear=True), \
                mock.patch.object(Liveness, "probe", side_effect=probe), \
                mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


# -- Claude Code ----------------------------------------------------------------------------------


def make_claude(env: Env, sid: str = SID, cwd: Optional[str] = None, title: str = "Fix the build",
                mtime: float = OLD) -> Dict[str, str]:
    """One Claude Code session with every artifact that carries its id, plus the project's memory.

    Returns {artifact name: path}. `cwd` defaults to an existing folder under HOME.
    """
    root = env.claude_dir
    if cwd is None:
        cwd = os.path.join(env.home, "dev", "proj")
        os.makedirs(cwd, exist_ok=True)
    project = os.path.join(root, "projects", enc(cwd))
    paths = {
        "transcript": os.path.join(project, sid + ".jsonl"),
        "subagents": os.path.join(project, sid),
        "memory": os.path.join(project, "memory"),
        "file-history": os.path.join(root, "file-history", sid),
        "session-env": os.path.join(root, "session-env", sid),
        # A job id unrelated to the session id, so only state.json's sessionId can tie them together.
        "job": os.path.join(root, "jobs", hashlib.sha1(sid.encode()).hexdigest()[:8]),
        "temp": os.path.join(env.claude_tmp, enc(cwd), sid),
        "cache-break": os.path.join(env.claude_tmp, f"cache-break-state-{sid}.json"),
    }
    lines = [{"type": "user", "cwd": cwd, "sessionId": sid}]
    if title:
        lines.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    touch(paths["transcript"], data="".join(json.dumps(line, separators=(",", ":")) + "\n"
                                            for line in lines).encode())
    touch(os.path.join(paths["subagents"], "subagents", "agent-1.jsonl"), 300)
    touch(os.path.join(paths["memory"], "MEMORY.md"), 50)
    touch(os.path.join(paths["file-history"], "abc@v1"), 200)
    touch(os.path.join(paths["session-env"], "env.sh"), 20)
    write_json(os.path.join(paths["job"], "state.json"), {"sessionId": sid, "state": "done", "cwd": cwd})
    touch(os.path.join(paths["temp"], "tasks", "out.txt"), 100)
    touch(paths["cache-break"], 10)
    for path in (root, env.claude_tmp):
        age_tree(path, mtime)
    return paths


def claude_source():
    from amnesia_sweep.sources import claude

    return claude.sources()[0]


# -- Codex ----------------------------------------------------------------------------------------


def make_rollout(env: Env, session_id: str, cwd: str, day: str = "2026/08/01", mtime: float = OLD) -> str:
    stamp = day.replace("/", "-")
    path = os.path.join(env.codex_dir, "sessions", day, f"rollout-{stamp}T10-00-00-{session_id}.jsonl")
    write_json(path, {"timestamp": f"{stamp}T10:00:00Z", "type": "session_meta",
                      "payload": {"id": session_id, "timestamp": f"{stamp}T10:00:00Z", "cwd": cwd,
                                  "originator": "codex_cli_rs"}}, mtime=mtime)
    return path


def make_codex(env: Env, releases: Iterable[str] = ("0.40.0", "0.41.0"), current: str = "0.41.0",
               mtime: float = OLD) -> Dict[str, str]:
    """A Codex home with installed releases, a database, auth and config. Returns named paths."""
    root = env.codex_dir
    paths = {}
    package = os.path.join(root, "packages", "standalone")
    for version in releases:
        paths[version] = os.path.dirname(touch(os.path.join(package, "releases", version, "codex"), 4096))
    if current:
        os.symlink(os.path.join("releases", current), os.path.join(package, "current"))
    paths["sqlite"] = touch(os.path.join(root, "state_5.sqlite"), 1024)
    paths["auth"] = write_json(os.path.join(root, "auth.json"), {"token": "secret"})
    paths["config"] = touch(os.path.join(root, "config.toml"), data=b'model = "o4"\n')
    age_tree(root, mtime)
    return paths


# -- git ------------------------------------------------------------------------------------------

GIT = shutil.which("git")


def git(cwd: str, *args: str) -> str:
    """Run real git in isolation from the user's config; raise on failure."""
    environ = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                           "-C", cwd, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=environ, check=False)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr.decode()}")
    return done.stdout.decode()


def make_git_repo(path: str) -> str:
    """A repository on branch main with one commit of README.md."""
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    touch(os.path.join(path, "README.md"), data=b"hello\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    return os.path.realpath(path)
