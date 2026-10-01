"""Run every source, assemble one tree, enforce the tree invariants, and overlay archive state.

Invariants: only leaves own paths, and no actionable path may equal or contain another listed
path (a containing item is made inactionable, so deleting it can't take something else with it).
Standard library only.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from .config import Config
from .env import Env
from .liveness import Liveness
from .model import ArchiveInfo, Node, finalize, iter_leaves, iter_nodes, make_inactionable, parent_map
from .sizing import Cancelled, Meter
from .sources import all_sources
from .sources.base import ScanContext, Source

ROOT_ID = ""


def registry_dirs(env: Env) -> List[str]:
    return [os.path.join(env.claude_dir, "sessions"), env.path(".openclaude", "sessions")]


def make_context(env: Env, config: Config, liveness: Optional[Liveness] = None,
                 cancel: Optional[threading.Event] = None) -> ScanContext:
    if liveness is None:
        liveness = Liveness.probe(registry_dirs(env))
    return ScanContext(env=env, config=config, liveness=liveness, meter=Meter(cancel))


def select_sources(ctx: ScanContext, names: Optional[List[str]] = None) -> List[Source]:
    chosen = []
    for source in all_sources():
        if names and source.name not in names:
            continue
        if source.name in ctx.config.disabled_sources:
            continue
        if source.present(ctx):
            chosen.append(source)
    return chosen


def _scan_one(source: Source, ctx: ScanContext) -> Node:
    try:
        node = source.scan(ctx)
    except Cancelled:
        raise
    except Exception as error:  # a broken source must not take the whole overview down
        node = Node(id=source.name, label=source.label, kind="tool", actionable=False,
                    reason=f"scan failed: {error}", flags={"keep-empty"},
                    meta={"error": f"{type(error).__name__}: {error}"})
    return finalize(node)


def iter_scan(ctx: ScanContext, sources: List[Source],
              meters: Optional[Dict[str, Meter]] = None, workers: int = 4) -> Iterator[Tuple[Source, Node]]:
    """Yield (source, finished node) pairs as each source completes."""
    meters = meters if meters is not None else {}
    contexts = {}
    for source in sources:
        meters.setdefault(source.name, Meter(ctx.meter.cancel))
        contexts[source.name] = dataclasses.replace(ctx, meter=meters[source.name])
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(sources) or 1))) as pool:
        futures = {pool.submit(_scan_one, s, contexts[s.name]): s for s in sources}
        for future in as_completed(futures):
            yield futures[future], future.result()


def worth_showing(node: Node) -> bool:
    """A source that found nothing (and didn't fail) isn't worth a row."""
    return bool(node.children or node.parts or node.meta.get("error"))


def assemble(nodes: List[Node], order: List[str]) -> Node:
    rank = {name: i for i, name in enumerate(order)}
    root = Node(id=ROOT_ID, label="All agents", kind="root",
                children=sorted((n for n in nodes if worth_showing(n)), key=lambda n: rank.get(n.id, len(rank))))
    warnings = enforce_invariants(root)
    finalize(root)
    root.meta["warnings"] = "\n".join(warnings)
    return root


def scan_all(ctx: ScanContext, sources: List[Source]) -> Node:
    nodes = [node for _, node in iter_scan(ctx, sources)]
    return assemble(nodes, [s.name for s in sources])


def enforce_invariants(root: Node) -> List[str]:
    """Make any leaf whose path equals or contains another listed path inactionable."""
    warnings = []
    owned: List[Tuple[str, Node]] = []
    for leaf in iter_leaves(root):
        for part in leaf.parts:
            owned.append((os.path.normpath(part.path), leaf))
    # Sort with the separator lowest so every path is immediately followed by its descendants.
    owned.sort(key=lambda pair: pair[0].replace(os.sep, "\0"))
    for i, (path, leaf) in enumerate(owned):
        prefix = path.rstrip(os.sep) + os.sep
        for other_path, other in owned[i + 1:]:
            if other_path != path and not other_path.startswith(prefix):
                break
            if other is leaf:
                continue
            target = leaf if other_path != path else other
            if target.actionable:
                make_inactionable(target, "overlaps another listed item")
                warnings.append(f"{target.id}: overlaps {other.id if target is leaf else leaf.id}")
    return warnings


def overlay_archive(root: Node, records: List[dict], retention_days: int) -> None:
    """Attach ArchiveInfo to every node whose paths are archived.

    A leaf is archived when one of its paths is in a record. A group is archived when all of its
    leaves are, and partially archived when only some are. `explicit` marks the node a record
    was made for, so the UI can tell "archived here" from "archived as part of something bigger".
    """
    from .archive import archived_ts, effective_retention, expires_at

    infos = {}
    for record in records:
        infos[record["id"]] = ArchiveInfo(
            archived_at=archived_ts(record), expires_at=expires_at(record, retention_days),
            retention_days=effective_retention(record, retention_days),
            pinned=record.get("retention_days") is not None)
    by_path = {part["path"]: record["id"] for record in records for part in record["parts"]}

    def visit(node: Node) -> Tuple[int, int]:
        """Return (archived leaves, total leaves) under node, setting node.archive on the way."""
        node.archive = None
        if not node.children:
            hit = next((by_path[p.path] for p in node.parts if p.path in by_path), None)
            if hit is not None:
                node.archive = dataclasses.replace(infos[hit], explicit=hit == node.id)
            return (1 if hit else 0), 1
        archived = total = 0
        first = None
        for child in node.children:
            a, t = visit(child)
            archived += a
            total += t
            if a and first is None and child.archive is not None:
                first = child.archive
        if archived:
            source = infos.get(node.id) or first
            node.archive = dataclasses.replace(source, explicit=node.id in infos, partial=archived < total)
        return archived, total

    visit(root)
    root.archive = None


def filter_older_than(root: Node, seconds: float, now: float) -> Node:
    """A copy of the tree with only leaves untouched for at least `seconds`."""
    clone = copy.deepcopy(root)
    cutoff = now - seconds

    def prune(node: Node) -> bool:
        if not node.children:
            return node.newest <= cutoff
        node.children = [c for c in node.children if prune(c)]
        return bool(node.children)

    prune(clone)
    return finalize(clone)
