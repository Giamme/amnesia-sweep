"""Git worktrees that AI agents created, found from the main repositories that own them.

`git worktree list --porcelain` is the authority on what is a worktree. A worktree counts as
agent-made when its path or branch matches a known agent pattern, it lives in a temp folder, or
a Codex session log shows Codex creating it. Removal goes through `git worktree remove` (never
plain rm), dirty worktrees need an explicit force, and locked or in-use ones are refused.
Standard library only.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Set, Tuple

from .model import Node, Part, make_inactionable
from .sources.base import ScanContext, Source, group, slug

SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".cache", "Library", ".Trash", "target",
             "dist", "build", ".git", ".hg", ".svn", ".next", ".turbo", "Pods", "DerivedData"}


class Git:
    """Thin wrapper over the git CLI with safe defaults (no prompts, C locale, timeouts)."""

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout

    def run(self, cwd: str, *args: str) -> Tuple[int, str, str]:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_OPTIONAL_LOCKS="0")
        try:
            done = subprocess.run(["git", "-C", cwd, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=self.timeout, env=env, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return 127, "", str(error)
        return done.returncode, done.stdout.decode("utf-8", "surrogateescape"), \
            done.stderr.decode("utf-8", "replace").strip()

    def worktrees(self, repo: str) -> Optional[List[dict]]:
        code, out, _ = self.run(repo, "worktree", "list", "--porcelain", "-z")
        if code != 0:
            return None
        records, current = [], {}
        for field in out.split("\0"):
            if not field:
                if current:
                    records.append(current)
                    current = {}
                continue
            key, _, value = field.partition(" ")
            if key == "worktree":
                current["path"] = value
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
            elif key in ("detached", "bare"):
                current[key] = True
            elif key in ("locked", "prunable"):
                current[key] = value or True
        if current:
            records.append(current)
        return records

    def dirty_count(self, path: str) -> Optional[int]:
        code, out, _ = self.run(path, "status", "--porcelain", "-z", "--untracked-files=normal")
        if code != 0:
            return None
        count = 0
        for entry in out.split("\0"):
            if len(entry) < 4:
                continue
            status, name = entry[:2], entry[3:]
            if status == "??" and os.path.islink(os.path.join(path, name.rstrip("/"))) \
                    and os.path.basename(name.rstrip("/")) == "node_modules":
                continue  # a symlink to a shared node_modules isn't work in progress
            count += 1
        return count

    def ahead(self, repo: str, branch: str, base: str) -> Optional[int]:
        code, out, _ = self.run(repo, "rev-list", "--count", f"{base}..refs/heads/{branch}")
        return int(out.strip()) if code == 0 and out.strip().isdigit() else None

    def check_removable(self, part: Part, force: bool) -> Optional[str]:
        """Re-check against git right before removal; return a refusal reason or None."""
        listed = self.worktrees(part.repo or "")
        if listed is None:
            return "can't list the repository's worktrees"
        real = os.path.realpath(part.path)
        for index, record in enumerate(listed):
            if os.path.realpath(record.get("path", "")) != real:
                continue
            if index == 0:
                return "that's the repository's main checkout"
            if record.get("locked"):
                return "the worktree is locked (an agent may still be using it)"
            if not force:
                dirty = self.dirty_count(part.path)
                if dirty is None:
                    return "can't read the worktree's status"
                if dirty:
                    return f"{dirty} uncommitted change(s); use force to remove anyway"
            return None
        return "git doesn't list this worktree any more"

    def remove(self, part: Part, force: bool, delete_branch: bool) -> None:
        args = ["worktree", "remove"] + (["--force"] if force else []) + [part.path]
        code, _, err = self.run(part.repo or "", *args)
        if code != 0:
            raise RuntimeError(err or "git worktree remove failed")
        self.prune(part.repo)
        if delete_branch and part.branch:
            self.run(part.repo or "", "branch", "-d", part.branch)  # -d refuses unmerged branches

    def prune(self, repo: Optional[str]) -> None:
        code, _, err = self.run(repo or "", "worktree", "prune")
        if code != 0:
            raise RuntimeError(err or "git worktree prune failed")


def _main_repo_of(git_file: str) -> Optional[str]:
    """Follow a worktree's `.git` file back to its main repository, or None."""
    try:
        with open(git_file, encoding="utf-8", errors="replace") as handle:
            text = handle.read(4096).strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = text[len("gitdir:"):].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(os.path.dirname(git_file), gitdir))
    if f"{os.sep}.git{os.sep}modules{os.sep}" in gitdir + os.sep:
        return None  # a submodule, not a worktree
    common = os.path.join(gitdir, "commondir")
    try:
        with open(common, encoding="utf-8") as handle:
            common_dir = os.path.normpath(os.path.join(gitdir, handle.read().strip()))
    except OSError:
        marker = f"{os.sep}.git{os.sep}worktrees{os.sep}"
        if marker not in gitdir:
            return None
        common_dir = gitdir.split(marker)[0] + os.sep + ".git"
    if os.path.basename(common_dir) != ".git":
        return None  # bare repositories have no main checkout to anchor to
    return os.path.realpath(os.path.dirname(common_dir))


def find_repos(roots: List[Tuple[str, int]]) -> Set[str]:
    """Main repositories under each (root, max depth), including those reached via worktrees."""
    repos: Set[str] = set()
    for root, max_depth in roots:
        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            marker = os.path.join(current, ".git")
            if os.path.isdir(marker) and not os.path.islink(marker):
                repos.add(os.path.realpath(current))
                continue
            if os.path.isfile(marker) and not os.path.islink(marker):
                main = _main_repo_of(marker)
                if main:
                    repos.add(main)
                continue
            if depth >= max_depth:
                continue
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.name in SKIP_DIRS or (entry.name.startswith(".") and depth > 0
                                                       and entry.name not in (".claude", ".forge-worktrees",
                                                                              ".worktrees", ".artifacts")):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append((entry.path, depth + 1))
                        except OSError:
                            continue
            except OSError:
                continue
    return repos


class _CodexProvenance:
    """Paths that Codex session logs show being passed to `git worktree add` (computed once)."""

    def __init__(self, sessions_dir: str):
        self.sessions_dir = sessions_dir
        self._paths: Optional[List[str]] = None
        self._lock = threading.Lock()

    def paths(self) -> List[str]:
        with self._lock:
            if self._paths is None:
                found: Set[str] = set()
                for path in glob.glob(os.path.join(glob.escape(self.sessions_dir), "**", "*.jsonl"), recursive=True):
                    try:
                        with open(path, "rb") as handle:
                            data = handle.read()
                    except OSError:
                        continue
                    for match in re.finditer(rb"worktree add", data):
                        tail = data[match.end():match.end() + 400].decode("utf-8", "replace")
                        tail = tail.replace("\\n", " ").replace('\\"', " ").replace("'", " ").replace('"', " ")
                        tokens = tail.split()
                        skip_next = False
                        for token in tokens:
                            if skip_next:
                                skip_next = False
                                continue
                            if token in ("-b", "-B", "--reason", "--orphan"):
                                skip_next = True
                                continue
                            if token.startswith("-"):
                                continue
                            token = token.rstrip(",;)")
                            while token.startswith("./"):
                                token = token[2:]
                            found.add(token)
                            break
                self._paths = sorted(p for p in found if p)
            return self._paths

    def created(self, worktree: str) -> bool:
        real = os.path.realpath(worktree)
        for candidate in self.paths():
            if candidate.startswith("/"):
                if os.path.realpath(candidate) == real or candidate == worktree:
                    return True
            elif real.endswith(os.sep + candidate.rstrip("/")):
                return True
        return False


def is_agent(path: str, branch: str, patterns: List[str], temp_roots: List[str]) -> Optional[str]:
    """Return why a worktree looks agent-made, or None."""
    for pattern in patterns:
        kind, _, value = pattern.partition(":")
        if kind == "path" and value and value in path + os.sep:
            return f"path contains {value.strip('/')}"
        if kind == "branch" and value and branch.startswith(value):
            return f"branch {value}*"
    for root in temp_roots:
        if path.startswith(root.rstrip(os.sep) + os.sep):
            return "lives in a temp folder"
    return None


class Worktrees(Source):
    name = "worktrees"
    label = "Git worktrees"

    def __init__(self, git: Optional[Git] = None):
        self.git = git or Git()
        self.repos: Set[str] = set()

    def search_roots(self, ctx: ScanContext) -> List[Tuple[str, int]]:
        env = ctx.env
        roots = [(os.path.realpath(os.path.expanduser(r.replace("~", env.home, 1) if r.startswith("~") else r)),
                  ctx.config.worktree_max_depth) for r in ctx.config.worktree_roots]
        roots += [(env.tmp_root, 2), (env.tmpdir, 2), (os.path.join(env.codex_dir, "worktrees"), 3),
                  (env.path("conductor"), 3), (env.path(".claude-squad", "worktrees"), 2),
                  (os.path.join(env.app_support, "SpankAI", "workspaces"), 3)]
        return [(r, d) for r, d in roots if os.path.isdir(r)]

    def present(self, ctx: ScanContext) -> bool:
        return bool(self.search_roots(ctx))

    def roots(self, ctx: ScanContext) -> List[str]:
        return []  # worktrees are removed through git, never through the filesystem roots

    def scan(self, ctx: ScanContext) -> Node:
        tool = group(self.name, self.label, kind="tool")
        provenance = _CodexProvenance(os.path.join(ctx.env.codex_dir, "sessions"))
        temp_roots = [ctx.env.tmp_root, ctx.env.tmpdir]
        found: List[Tuple[str, dict, str, Optional[str], str]] = []  # (repo, record, path, why, main head)
        owners: Set[str] = set()
        for repo in sorted(find_repos(self.search_roots(ctx))):
            listed = self.git.worktrees(repo)
            if not listed or len(listed) < 2:
                continue
            owners.add(repo)  # only repositories that own worktrees matter to the guard
            main_head = listed[0].get("branch") or listed[0].get("head") or "HEAD"
            for record in listed[1:]:
                if record.get("bare"):
                    continue
                path = os.path.realpath(record.get("path", ""))
                branch = record.get("branch", "")
                why = is_agent(path, branch, ctx.config.worktree_agent_patterns, temp_roots)
                if why is None and provenance.created(path):
                    why = "created by Codex (per its session log)"
                if why is None and not ctx.config.all_worktrees:
                    continue
                found.append((repo, record, path, why, main_head))

        self.repos = owners

        def build(item) -> Tuple[str, Node]:
            repo, record, path, why, main_head = item
            return repo, self._leaf(ctx, repo, record, path, why, main_head)

        with ThreadPoolExecutor(max_workers=6) as pool:
            built = list(pool.map(build, found))
        groups: Dict[str, Node] = {}
        for repo, leaf in built:
            parent = groups.get(repo)
            if parent is None:
                parent = tool.add(group(f"{self.name}/{slug(ctx.env.tilde(repo))}", ctx.env.tilde(repo),
                                        kind="project", repo=repo, path=repo))
                groups[repo] = parent
            parent.add(leaf)
        return tool

    def _leaf(self, ctx: ScanContext, repo: str, record: dict, path: str, why: Optional[str],
              main_head: str) -> Node:
        branch = record.get("branch", "")
        admin = _admin_name(path) or os.path.basename(path)
        label = ctx.env.tilde(path)
        if branch:
            label += f"  [{branch}]"
        elif record.get("detached"):
            label += "  [detached]"
        node_id = f"{self.name}/{slug(ctx.env.tilde(repo))}/{slug(admin)}"
        meta = {"repo": repo, "path": path, "branch": branch, "head": record.get("head", ""),
                "agent": why or "", "session_id": ""}
        if record.get("prunable"):
            part = Part(path=path, missing=True, remover="git-prune", repo=repo, branch=branch or None)
            node = Node(id=node_id, label=label + "  (folder is gone)", kind="worktree", risk="worktree",
                        parts=[part], flags={"prunable"}, meta=meta)
            return node
        part = ctx.measure(path, remover="git-worktree", repo=repo, branch=branch or None)
        node = Node(id=node_id, label=label, kind="worktree", risk="worktree", parts=[part],
                    meta={k: v for k, v in meta.items() if v})
        if why is None:
            node.flags.add("not-agent")
        if record.get("locked"):
            reason = record["locked"] if isinstance(record["locked"], str) else ""
            make_inactionable(node, "locked" + (f": {reason}" if reason else ""), "locked")
            return node
        if ctx.liveness.path_in_use(path):
            make_inactionable(node, "a running process is working in it", "live")
            return node
        if ctx.recent(part.newest):
            make_inactionable(node, "changed in the last few minutes", "recent")
        dirty = self.git.dirty_count(path)
        if dirty is None:
            make_inactionable(node, "git can't read its status")
        elif dirty:
            node.flags.add("dirty")
            node.meta["dirty"] = str(dirty)
        if branch:
            ahead = self.git.ahead(repo, branch, main_head)
            if ahead:
                node.flags.add("unmerged")
                node.meta["ahead"] = str(ahead)
        return node


def _admin_name(path: str) -> Optional[str]:
    try:
        with open(os.path.join(path, ".git"), encoding="utf-8", errors="replace") as handle:
            text = handle.read(4096).strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    return os.path.basename(text[len("gitdir:"):].strip().rstrip("/"))


def sources() -> List[Source]:
    return [Worktrees()]
