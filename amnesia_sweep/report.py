"""Text and JSON renderings of the scan tree, plus the size and age formats used everywhere.

Sizes use 1024-based units (KiB, MiB, GiB) to match what `du -h` and Finder's "on disk" report.
Standard library only.
"""

from __future__ import annotations

import re
from typing import List, Optional

from .model import Node, iter_leaves, sort_children

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*$", re.IGNORECASE)
_AGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|min|h|d|w|mo|y)?\s*$", re.IGNORECASE)
_AGE_SECONDS = {"s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400,
                "y": 365 * 86400}

FLAG_TEXT = (("live", "LIVE"), ("job-active", "JOB"), ("orphan", "ORPHAN"), ("dirty", "DIRTY"),
             ("locked", "LOCKED"), ("unmerged", "UNMERGED"), ("prunable", "PRUNABLE"),
             ("current", "CURRENT"), ("in-use", "IN-USE"), ("recent", "RECENT"))


def human_size(n: int) -> str:
    value = float(n)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def human_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 60 * 86400:
        return f"{int(seconds // 86400)}d"
    if seconds < 365 * 86400:
        return f"{int(seconds // (30 * 86400))}mo"
    return f"{seconds / (365 * 86400):.1f}y"


def parse_size(text: str) -> int:
    match = _SIZE.match(text)
    if not match:
        raise ValueError(f"not a size: {text!r} (try 500M or 2G)")
    power = " KMGT".index(match.group(2).upper() or " ")
    return int(float(match.group(1)) * 1024 ** power)


def parse_age(text: str) -> float:
    match = _AGE.match(text)
    if not match:
        raise ValueError(f"not an age: {text!r} (try 30d, 2w or 6h)")
    return float(match.group(1)) * _AGE_SECONDS[(match.group(2) or "d").lower()]


def node_age(node: Node, now: float) -> str:
    return human_age(now - node.newest) if node.newest else "-"


def flags_text(node: Node, now: float) -> str:
    bits = [text for flag, text in FLAG_TEXT if flag in node.flags]
    if node.archive is not None:
        days = node.archive.days_left(now)
        tag = "due" if days <= 0 else f"{int(days + 0.999)}d"
        bits.append(f"A{'~' if node.archive.partial else ''} {tag}")
    if node.risk == "protected" and not node.children:
        bits.append("KEPT")
    return " ".join(bits)


def totals(root: Node) -> dict:
    archived = 0
    for leaf in iter_leaves(root):
        if leaf.archive is not None:
            archived += leaf.bytes
    return {"bytes": root.bytes, "reclaimable": root.reclaimable, "archived": archived,
            "kept": root.bytes - root.reclaimable}


def render_text(root: Node, now: float, depth: int = 2, min_size: int = 0, show_ids: bool = False,
                sort: str = "size", top: int = 10, warnings: Optional[List[str]] = None) -> str:
    t = totals(root)
    lines = [f"Total {human_size(t['bytes'])} · reclaimable {human_size(t['reclaimable'])} · "
             f"archived {human_size(t['archived'])} · kept {human_size(t['kept'])}", ""]
    header = f"{'SIZE':>10} {'%':>4} {'AGE':>5}  {'FLAGS':<16} ITEM"
    lines.append(header)
    total = root.bytes or 1

    def walk(node: Node, level: int) -> None:
        for child in sort_children(node.children, sort):
            if child.bytes < min_size and child.kind != "tool":
                continue
            label = "  " * level + child.label
            if child.meta.get("error"):
                label += f"  [scan failed: {child.meta['error']}]"
            row = (f"{human_size(child.bytes):>10} {100 * child.bytes / total:>3.0f}% {node_age(child, now):>5}  "
                   f"{flags_text(child, now):<16} {label}")
            if show_ids:
                row += f"  [{child.id}]"
            lines.append(row.rstrip())
            if level + 1 < depth:
                walk(child, level + 1)

    walk(root, 0)
    leaves = sorted((l for l in iter_leaves(root) if l.actionable and l.archive is None),
                    key=lambda l: -l.bytes)[:top]
    if leaves and top:
        lines += ["", "Largest reclaimable items"]
        for leaf in leaves:
            lines.append(f"{human_size(leaf.bytes):>10} {node_age(leaf, now):>5}  {leaf.id}")
    if warnings:
        lines += ["", *(f"warning: {w}" for w in warnings)]
    return "\n".join(lines)


def node_json(node: Node, now: float, depth: Optional[int] = None) -> dict:
    data = {
        "id": node.id, "label": node.label, "kind": node.kind, "risk": node.risk,
        "bytes": node.bytes, "files": node.files, "newest": node.newest or None,
        "reclaimable": node.reclaimable, "actionable": node.actionable, "reason": node.reason or None,
        "flags": sorted(f for f in node.flags if f != "keep-empty"), "meta": node.meta,
        "paths": [p.path for p in node.parts],
        "archive": None if node.archive is None else {
            "archived_at": node.archive.archived_at, "expires_at": node.archive.expires_at,
            "days_left": round(node.archive.days_left(now), 2), "retention_days": node.archive.retention_days,
            "explicit": node.archive.explicit, "partial": node.archive.partial},
    }
    if depth is None or depth > 0:
        data["children"] = [node_json(c, now, None if depth is None else depth - 1)
                            for c in sort_children(node.children)]
    else:
        data["children"] = []
    return data
