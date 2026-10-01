"""The guard every deletion passes through, independently of how the scan built its tree.

A source bug can hide data or mislabel it, but it can't make this module approve deleting a path
outside the known agent folders, a protected path, a folder that contains a protected path,
HOME itself, or anything reached through a symlinked parent. Standard library only.
"""

from __future__ import annotations

import fnmatch
import glob
import os
from typing import Iterable, List, Optional, Sequence

from .env import Env
from .model import Part


def _real(path: str) -> str:
    return os.path.realpath(path)


def _inside(path: str, root: str) -> bool:
    """True when path is root itself or somewhere under it."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class Guard:
    def __init__(self, env: Env, roots: Iterable[str], protected: Iterable[str],
                 worktree_roots: Iterable[str] = (), repos: Iterable[str] = (), exclude: Iterable[str] = ()):
        self.env = env
        self.roots = sorted({_real(r) for r in roots if r})
        self.protected_patterns = [p for p in protected if p] + [os.path.expanduser(p) for p in exclude]
        self.repos = {_real(r) for r in repos}
        home = _real(env.home)
        denied = {"/", "/Users", "/private", "/private/tmp", "/private/var", "/tmp", "/var",
                  _real(env.tmp_root), _real(env.tmpdir), home, _real(env.state_dir), _real(env.config_dir)}
        for name in ("Desktop", "Documents", "Downloads", "Library", ".ssh", ".gnupg", ".config", ".local",
                     "dev", ".local/share", ".local/state", ".local/bin", "Library/Caches",
                     "Library/Application Support"):
            denied.add(os.path.join(home, name))
        denied.update(self.roots)
        denied.update(_real(os.path.expanduser(r)) for r in worktree_roots)
        self.denied = denied
        self._protected_cache: Optional[List[str]] = None

    def protected_paths(self) -> List[str]:
        """Expand the protected patterns into the concrete paths that exist right now."""
        if self._protected_cache is None:
            found = set()
            for pattern in self.protected_patterns:
                for hit in glob.glob(pattern):
                    found.add(os.path.normpath(hit))
                    found.add(_real(hit))
            self._protected_cache = sorted(found)
        return self._protected_cache

    def is_protected(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, p) for p in self.protected_patterns)

    def check(self, part: Part) -> Optional[str]:
        """Return why this part must not be removed, or None if removing it is allowed."""
        path = part.path
        if not os.path.isabs(path) or os.path.normpath(path) != path or ".." in path.split(os.sep):
            return "not a clean absolute path"
        if len([p for p in path.split(os.sep) if p]) < 3:
            return "path is too short to be an agent leftover"
        real_parent = _real(os.path.dirname(path))
        candidate = os.path.join(real_parent, os.path.basename(path))
        if path in self.denied or candidate in self.denied or _real(path) in self.denied:
            return "refusing to delete a top-level or system folder"
        if part.remover == "fs":
            if not any(_inside(real_parent, root) for root in self.roots):
                return "outside the known agent folders"
        elif part.remover in ("git-worktree", "git-prune"):
            if not part.repo or _real(part.repo) not in self.repos:
                return "not a worktree of a repository found in this scan"
            if _real(path) == _real(part.repo):
                return "that's the repository's main checkout"
        else:
            return f"unknown remover {part.remover!r}"
        if self.is_protected(path) or self.is_protected(candidate):
            return "protected"
        prefix = candidate.rstrip(os.sep) + os.sep
        plain = path.rstrip(os.sep) + os.sep
        for protected in self.protected_paths():
            if protected.startswith(prefix) or protected.startswith(plain):
                return f"contains a protected path ({protected})"
            if _inside(candidate, protected) or _inside(path, protected):
                return f"inside a protected path ({protected})"
        for repo in self.repos:
            if part.remover == "fs" and (_inside(repo, candidate) or _inside(candidate, repo)):
                return "inside or containing a git repository's main checkout"
        return None


def guard_for(env: Env, sources: Sequence, ctx, repos: Iterable[str] = ()) -> Guard:
    roots: List[str] = []
    protected: List[str] = []
    for source in sources:
        roots.extend(source.roots(ctx))
        protected.extend(source.protected(ctx))
    return Guard(env, roots, protected, worktree_roots=ctx.config.worktree_roots, repos=repos,
                 exclude=ctx.config.exclude)
