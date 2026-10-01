"""The scan tree: Parts (filesystem objects an action owns) hang off leaf Nodes.

Groups never own paths. Acting on a group means acting on each actionable leaf under it, so a
group action can never sweep up something a source deliberately left out (such as a Claude
project's memory/ folder). Standard library only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Set

# Ordered from "safe to lose" to "never touch".
RISKS = ("ephemeral", "cache", "history", "binary", "worktree", "model", "user", "shared", "protected")

MARKS = ("delete", "archive", "unarchive", "keep")


@dataclass
class Part:
    """One file, directory, symlink or worktree that an action removes as a unit."""

    path: str
    bytes: int = 0
    files: int = 0
    newest: float = 0.0
    errors: int = 0
    missing: bool = False
    remover: str = "fs"            # fs | git-worktree | git-prune
    repo: Optional[str] = None     # main repository, for the git removers
    branch: Optional[str] = None   # worktree branch, for optional `git branch -d`

    def fingerprint(self) -> dict:
        return {"bytes": self.bytes, "files": self.files, "newest": self.newest}

    def to_dict(self) -> dict:
        data = {"path": self.path, "remover": self.remover, "repo": self.repo}
        if self.branch:
            data["branch"] = self.branch
        data.update(self.fingerprint())
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Part":
        return cls(path=data["path"], bytes=int(data.get("bytes", 0)), files=int(data.get("files", 0)),
                   newest=float(data.get("newest", 0.0)), remover=data.get("remover", "fs"),
                   repo=data.get("repo"), branch=data.get("branch"))


@dataclass
class ArchiveInfo:
    archived_at: float
    expires_at: float
    retention_days: int
    pinned: bool = False
    partial: bool = False          # only some of this node's leaves are archived
    explicit: bool = True          # this node is itself an archive record (not just under one)

    def days_left(self, now: float) -> float:
        return (self.expires_at - now) / 86400.0


@dataclass
class Node:
    id: str
    label: str
    kind: str = "item"             # tool | category | project | session | artifact | version | worktree | item
    risk: str = "ephemeral"
    parts: List[Part] = field(default_factory=list)
    children: List["Node"] = field(default_factory=list)
    actionable: bool = True
    reason: str = ""
    flags: Set[str] = field(default_factory=set)
    meta: Dict[str, str] = field(default_factory=dict)
    # Filled in by finalize().
    bytes: int = 0
    files: int = 0
    newest: float = 0.0
    reclaimable: int = 0
    archive: Optional[ArchiveInfo] = None

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def add(self, child: "Node") -> "Node":
        self.children.append(child)
        return child

    def child(self, id_suffix: str) -> Optional["Node"]:
        wanted = f"{self.id}/{id_suffix}"
        for child in self.children:
            if child.id == wanted:
                return child
        return None


# Flags that bubble up from leaves so a collapsed group still shows them.
_BUBBLING = ("live", "dirty", "locked", "job-active")


def finalize(node: Node) -> Node:
    """Aggregate sizes, ages and actionability bottom-up. Empty groups are dropped."""
    if node.children:
        kept = []
        for child in node.children:
            finalize(child)
            if child.children or child.parts or child.kind in ("tool",) or "keep-empty" in child.flags:
                kept.append(child)
        node.children = kept
    if node.parts and node.children:
        raise ValueError(f"node {node.id} has both parts and children")
    if node.children:
        node.bytes = sum(c.bytes for c in node.children)
        node.files = sum(c.files for c in node.children)
        node.newest = max((c.newest for c in node.children), default=0.0)
        node.reclaimable = sum(c.reclaimable for c in node.children)
        node.actionable = any(c.actionable for c in node.children)
        if not node.actionable and not node.reason:
            reasons = {c.reason for c in node.children if c.reason}
            node.reason = reasons.pop() if len(reasons) == 1 else "nothing here can be removed"
        for child in node.children:
            node.flags.update(f for f in child.flags if f in _BUBBLING)
    else:
        node.bytes = sum(p.bytes for p in node.parts)
        node.files = sum(p.files for p in node.parts)
        node.newest = max((p.newest for p in node.parts), default=0.0)
        if not node.parts:
            node.actionable = False
            node.reason = node.reason or "nothing on disk"
        node.reclaimable = node.bytes if node.actionable else 0
    return node


def make_inactionable(node: Node, reason: str, flag: Optional[str] = None) -> None:
    """Mark a node and everything under it as not actionable, with this as the reason."""
    for each in iter_nodes(node):
        each.actionable = False
        each.reason = reason
        if flag:
            each.flags.add(flag)


def iter_nodes(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def iter_leaves(node: Node) -> Iterator[Node]:
    for each in iter_nodes(node):
        if not each.children:
            yield each


def find(root: Node, node_id: str) -> Optional[Node]:
    for each in iter_nodes(root):
        if each.id == node_id:
            return each
    return None


def parent_map(root: Node) -> Dict[str, str]:
    parents: Dict[str, str] = {}
    for each in iter_nodes(root):
        for child in each.children:
            parents[child.id] = each.id
    return parents


def ancestors(node_id: str, parents: Dict[str, str]) -> Iterator[str]:
    current = parents.get(node_id)
    while current is not None:
        yield current
        current = parents.get(current)


def effective_mark(node_id: str, marks: Dict[str, str], parents: Dict[str, str]):
    """Return (mark, explicit) for a node: its own mark, or the nearest marked ancestor's.

    A "keep" mark cancels anything inherited from above, and resolves to (None, True).
    """
    if node_id in marks:
        mark = marks[node_id]
        return (None, True) if mark == "keep" else (mark, True)
    for above in ancestors(node_id, parents):
        if above in marks:
            mark = marks[above]
            return (None, False) if mark == "keep" else (mark, False)
    return (None, False)


def resolve_target(root: Node, token: str) -> List[Node]:
    """Find nodes for a user-typed target: an exact id, a path a leaf owns, or a unique prefix.

    The prefix form matches the start of an id's last segment (e.g. the first 8 hex digits of a
    session id) and needs at least 4 characters. Returns every candidate; more than one means
    the token is ambiguous.
    """
    exact = find(root, token)
    if exact is not None:
        return [exact]
    if token.startswith(os.sep) or token.startswith("~"):
        wanted = os.path.realpath(os.path.expanduser(token))
        hits = []
        for leaf in iter_leaves(root):
            for part in leaf.parts:
                if os.path.realpath(part.path) == wanted or part.path == token:
                    hits.append(leaf)
                    break
        if hits:
            return hits
        for each in iter_nodes(root):
            if each.meta.get("path") and os.path.realpath(each.meta["path"]) == wanted:
                return [each]
        return []
    if len(token) < 4:
        return []
    return [each for each in iter_nodes(root) if each.id.rsplit("/", 1)[-1].startswith(token)]


def sort_children(nodes: List[Node], key: str = "size") -> List[Node]:
    if key == "age":
        return sorted(nodes, key=lambda n: (n.newest or float("inf"), n.label))
    if key == "name":
        return sorted(nodes, key=lambda n: n.label.lower())
    return sorted(nodes, key=lambda n: (-n.bytes, n.label.lower()))
