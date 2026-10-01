"""What every harness source shares: the scan context, node-building helpers, and declarative rules.

A source turns one tool's on-disk layout into a Node tree. It names the roots it may delete under
and the paths that must never be touched; safety.Guard enforces both again at delete time, so a
mistake in a source can hide data but can't widen what gets deleted. Standard library only.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from ..config import Config
from ..env import Env
from ..liveness import Liveness
from ..model import Node, Part, make_inactionable
from ..sizing import Meter, measure


@dataclass
class ScanContext:
    env: Env
    config: Config
    liveness: Liveness
    meter: Meter = field(default_factory=Meter)

    def measure(self, path: str, **part_fields) -> Part:
        return measure(path, self.meter, **part_fields)

    def recent(self, newest: float) -> bool:
        return newest > 0 and self.env.now() - newest < self.config.active_grace_minutes * 60

    def excluded(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, os.path.expanduser(p)) for p in self.config.exclude)


class Source:
    """One harness. Subclasses fill in name/label and implement present() and scan()."""

    name = ""
    label = ""

    def present(self, ctx: ScanContext) -> bool:
        return any(os.path.isdir(root) for root in self.roots(ctx))

    def roots(self, ctx: ScanContext) -> List[str]:
        """Directories this source may delete inside of (never the roots themselves)."""
        return []

    def protected(self, ctx: ScanContext) -> List[str]:
        """Absolute glob patterns of paths that must never be deleted or contain a deletion."""
        return []

    def scan(self, ctx: ScanContext) -> Node:
        raise NotImplementedError


def ls(path: str) -> List[os.DirEntry]:
    try:
        with os.scandir(path) as entries:
            return sorted(entries, key=lambda e: e.name)
    except OSError:
        return []


def is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def leaf(ctx: ScanContext, node_id: str, label: str, path: str, risk: str = "ephemeral",
         kind: str = "item", min_age_hours: float = 0, **meta: str) -> Node:
    """A leaf owning one measured path, made inactionable if it's recent or excluded."""
    part = ctx.measure(path)
    node = Node(id=node_id, label=label, kind=kind, risk=risk, parts=[part],
                meta={k: v for k, v in meta.items() if v})
    node.meta.setdefault("path", path)
    if ctx.excluded(path):
        make_inactionable(node, "excluded by config")
    elif min_age_hours and part.newest and ctx.env.now() - part.newest < min_age_hours * 3600:
        make_inactionable(node, f"newer than {min_age_hours:g}h", "recent")
    elif ctx.recent(part.newest):
        make_inactionable(node, "changed in the last few minutes", "recent")
    return node


def multi_leaf(ctx: ScanContext, node_id: str, label: str, paths: Iterable[str], risk: str,
               kind: str = "item", **meta: str) -> Node:
    """A leaf owning several measured paths that are removed together."""
    parts = [ctx.measure(p) for p in paths]
    node = Node(id=node_id, label=label, kind=kind, risk=risk, parts=parts,
                meta={k: v for k, v in meta.items() if v})
    newest = max((p.newest for p in parts), default=0.0)
    if any(ctx.excluded(p.path) for p in parts):
        make_inactionable(node, "excluded by config")
    elif ctx.recent(newest):
        make_inactionable(node, "changed in the last few minutes", "recent")
    return node


def protected_leaf(ctx: ScanContext, node_id: str, label: str, path: str,
                   reason: str = "protected", risk: str = "protected") -> Node:
    node = Node(id=node_id, label=label, kind="item", risk=risk, parts=[ctx.measure(path)],
                actionable=False, reason=reason, meta={"path": path})
    return node


def group(node_id: str, label: str, kind: str = "category", **meta: str) -> Node:
    return Node(id=node_id, label=label, kind=kind, meta={k: v for k, v in meta.items() if v})


def slug(text: str) -> str:
    """A path-safe id segment."""
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in text).strip("-")
    return cleaned or "-"


def unclaimed(root: str, claimed: Iterable[str]) -> List[str]:
    """Top-level entries of root that no claimed path is equal to or inside of."""
    claimed = [os.path.normpath(p) for p in claimed]
    rest = []
    for entry in ls(root):
        path = os.path.normpath(entry.path)
        prefix = path + os.sep
        if any(c == path or c.startswith(prefix) for c in claimed):
            continue
        rest.append(entry.path)
    return rest


def other_bucket(ctx: ScanContext, parent_id: str, paths: Iterable[str],
                 label: str = "Other (not touched)") -> Optional[Node]:
    """Default-deny: whatever a source doesn't recognise is shown but never actionable."""
    paths = list(paths)
    if not paths:
        return None
    bucket = group(f"{parent_id}/other", label)
    for path in paths:
        bucket.add(protected_leaf(ctx, f"{parent_id}/other/{slug(os.path.basename(path))}",
                                  os.path.basename(path), path, reason="not recognised; left alone"))
    return bucket


# ---------------------------------------------------------------------------------------------
# Declarative sources for tools whose layout is just "folder + a few known subfolders".


@dataclass(frozen=True)
class Rule:
    pattern: str                 # glob relative to the source root
    category: str                # category label, e.g. "Sessions", "Logs"
    risk: str = "cache"
    requires: str = ""           # Config attribute that must be true for the match to be actionable
    min_age_hours: float = 0
    group_parent: bool = False   # group matches under their parent folder's name
    decode: str = ""             # "url": percent-decode the parent folder name for its label


@dataclass
class SpecSource(Source):
    name: str = ""
    label: str = ""
    root: str = ""               # relative to HOME, or absolute
    rules: Tuple[Rule, ...] = ()
    keep: Tuple[str, ...] = ()   # extra relative globs that are protected (documented, never matched)

    def base(self, ctx: ScanContext) -> str:
        return self.root if os.path.isabs(self.root) else ctx.env.path(self.root)

    def roots(self, ctx: ScanContext) -> List[str]:
        return [self.base(ctx)]

    def protected(self, ctx: ScanContext) -> List[str]:
        return [os.path.join(self.base(ctx), k) for k in self.keep]

    def scan(self, ctx: ScanContext) -> Node:
        base = self.base(ctx)
        tool = group(self.name, self.label, kind="tool", path=base)
        categories: Dict[str, Node] = {}
        claimed: List[str] = []
        keep_globs = self.protected(ctx)
        for rule in self.rules:
            for path in sorted(glob.glob(os.path.join(glob.escape(base), rule.pattern))):
                if any(fnmatch.fnmatch(path, k) for k in keep_globs):
                    continue
                claimed.append(path)
                cat_slug = slug(rule.category.lower())
                category = categories.get(rule.category)
                if category is None:
                    category = tool.add(group(f"{self.name}/{cat_slug}", rule.category))
                    categories[rule.category] = category
                rel = os.path.relpath(path, base)
                parent = category
                if rule.group_parent:
                    folder = os.path.basename(os.path.dirname(path))
                    label = urllib.parse.unquote(folder) if rule.decode == "url" else folder
                    label = ctx.env.tilde(label)
                    parent = category.child(slug(folder)) or category.add(
                        group(f"{category.id}/{slug(folder)}", label, kind="project"))
                    node_id = f"{parent.id}/{slug(os.path.basename(path))}"
                    node_label = os.path.basename(path)
                else:
                    node_id = f"{category.id}/{slug(rel)}"
                    node_label = rel
                node = leaf(ctx, node_id, node_label, path, risk=rule.risk,
                            min_age_hours=rule.min_age_hours)
                if rule.requires and not getattr(ctx.config, rule.requires, False):
                    make_inactionable(node, f"needs --{rule.requires.replace('_', '-')}")
                parent.add(node)
        rest = unclaimed(base, claimed)
        bucket = other_bucket(ctx, self.name, rest)
        if bucket is not None:
            tool.add(bucket)
        return tool
