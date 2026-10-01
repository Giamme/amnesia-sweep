"""Turning marks into a plan, carrying the plan out, and the audit log of everything done.

A mark on a group expands to the actionable leaves under it; anything else is listed as skipped
with its reason. Every path is checked by safety.Guard again immediately before it is removed,
liveness is re-probed once per run, and one failure never stops the rest. Standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .archive import ArchiveStore
from .env import Env, iso
from .model import Node, Part, iter_leaves, iter_nodes, parent_map
from .safety import Guard
from .sizing import changed, measure


@dataclass
class Op:
    action: str                    # delete | archive | unarchive | expire-delete
    node_id: str
    label: str
    source: str
    parts: List[Part]
    bytes: int = 0
    session_id: str = ""
    flags: Set[str] = field(default_factory=set)
    retention_days: Optional[int] = None
    force: bool = False            # remove a dirty worktree anyway
    delete_branch: bool = False


@dataclass
class Skip:
    node_id: str
    label: str
    reason: str
    bytes: int = 0


@dataclass
class Plan:
    ops: List[Op] = field(default_factory=list)
    skipped: List[Skip] = field(default_factory=list)

    def of(self, action: str) -> List[Op]:
        return [op for op in self.ops if op.action == action]

    def total(self, action: str) -> int:
        return sum(op.bytes for op in self.of(action))

    def skipped_by_reason(self) -> List[Tuple[str, int, int]]:
        """(reason, count, bytes) sorted by bytes, for the confirm summary."""
        grouped: Dict[str, List[int]] = {}
        for skip in self.skipped:
            entry = grouped.setdefault(skip.reason, [0, 0])
            entry[0] += 1
            entry[1] += skip.bytes
        return sorted(((r, c, b) for r, (c, b) in grouped.items()), key=lambda x: -x[2])


@dataclass
class Result:
    op: Op
    ok: bool
    error: str = ""
    freed: int = 0
    skipped: bool = False


def breadcrumb(node_id: str, index: Dict[str, Node], parents: Dict[str, str]) -> str:
    names = []
    current: Optional[str] = node_id
    while current:
        node = index.get(current)
        if node is None:
            break
        names.append(node.label.split("  ")[0] if node.kind == "session" else node.label)
        current = parents.get(current)
    return " › ".join(reversed(names))


def _origin(node_id: str, marks: Dict[str, str], parents: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """The effective mark of a node and the id of the node carrying it."""
    current: Optional[str] = node_id
    while current is not None:
        if current in marks:
            mark = marks[current]
            return (None, current) if mark == "keep" else (mark, current)
        current = parents.get(current)
    return None, None


def build_plan(root: Node, marks: Dict[str, str], retention_days: Optional[int] = None,
               force: Iterable[str] = (), delete_branches: bool = False) -> Plan:
    plan = Plan()
    parents = parent_map(root)
    index = {n.id: n for n in iter_nodes(root)}
    force = set(force)

    def top(node_id: str) -> str:
        current = node_id
        while parents.get(current) not in (None, root.id):
            current = parents[current]
        return current

    def session_of(node_id: str) -> str:
        current: Optional[str] = node_id
        while current is not None:
            sid = index[current].meta.get("session_id") if current in index else None
            if sid:
                return sid
            current = parents.get(current)
        return ""

    grouped: Dict[Tuple[str, str], List[Node]] = {}
    for leaf in iter_leaves(root):
        mark, origin = _origin(leaf.id, marks, parents)
        if mark is None:
            continue
        marked = index[origin]
        crumb = breadcrumb(leaf.id, index, parents)
        if mark in ("delete", "archive") and not leaf.actionable:
            plan.skipped.append(Skip(leaf.id, crumb, leaf.reason or "not actionable", leaf.bytes))
            continue
        if mark == "delete" and leaf.archive is not None and (marked.archive is None or marked.archive.partial):
            plan.skipped.append(Skip(leaf.id, crumb, "archived (mark it directly to delete it now)", leaf.bytes))
            continue
        if mark == "archive" and leaf.archive is not None and marked.archive is None:
            plan.skipped.append(Skip(leaf.id, crumb, "already archived", leaf.bytes))
            continue
        if mark == "unarchive" and leaf.archive is None:
            continue
        if mark == "delete":
            plan.ops.append(Op("delete", leaf.id, crumb, top(leaf.id), list(leaf.parts), leaf.bytes,
                               session_of(leaf.id), set(leaf.flags),
                               force=leaf.id in force or origin in force,
                               delete_branch=delete_branches))
        else:
            grouped.setdefault((mark, origin), []).append(leaf)
    for (mark, origin), leaves in grouped.items():
        parts = [p for leaf in leaves for p in leaf.parts]
        plan.ops.append(Op(mark, origin, breadcrumb(origin, index, parents), top(origin), parts,
                           sum(l.bytes for l in leaves), session_of(origin),
                           retention_days=retention_days if mark == "archive" else None))
    return plan


# -- the audit log ----------------------------------------------------------------------------------


def append_log(state_dir: str, entry: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    entry = {"ts": iso(time.time()), **entry}
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    fd = os.open(os.path.join(state_dir, "log.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def log_events(state_dir: str, events: List) -> None:
    for event in events:
        append_log(state_dir, {"action": "auto-unarchive" if event.kind == "unarchived" else "gone",
                               "id": event.record_id, "label": event.label, "paths": [event.path],
                               "result": "ok"})


def read_log(state_dir: str, limit: int = 50) -> List[dict]:
    path = os.path.join(state_dir, "log.jsonl")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    entries = []
    for line in lines[-limit:] if limit else lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


# -- removal ------------------------------------------------------------------------------------------


def _retry_writable(func, path, _exc_info):
    """rmtree error hook: make the parent (and the entry) writable once, then retry."""
    parent = os.path.dirname(path)
    try:
        os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
        if not os.path.islink(path):
            os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    func(path)


def remove_path(path: str) -> None:
    """Delete one file, symlink or directory tree without following any symlink."""
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path, onerror=_retry_writable)
    else:
        os.unlink(path)


ProbeLive = Callable[[], "object"]


class Executor:
    def __init__(self, env: Env, guard: Guard, store: ArchiveStore, dry_run: bool = False,
                 grace_minutes: int = 15, probe_live: Optional[ProbeLive] = None, git=None):
        from . import worktrees

        self.env = env
        self.guard = guard
        self.store = store
        self.dry_run = dry_run
        self.grace = grace_minutes * 60
        self.probe_live = probe_live
        self.git = git or worktrees.Git()
        self._live = None

    def live(self):
        if self._live is None and self.probe_live is not None:
            self._live = self.probe_live()
        return self._live

    def log(self, op: Op, result: str, error: str = "", freed: int = 0) -> None:
        if self.dry_run:
            return
        append_log(self.env.state_dir, {"action": op.action, "id": op.node_id, "label": op.label,
                                        "paths": [p.path for p in op.parts], "bytes": freed or op.bytes,
                                        "result": result, "error": error or None})

    def run(self, plan: Plan, on_progress: Optional[Callable[[int, int, Op], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None) -> List[Result]:
        results = []
        for number, op in enumerate(plan.ops, 1):
            if should_stop is not None and should_stop():
                break
            if on_progress is not None:
                on_progress(number, len(plan.ops), op)
            try:
                if op.action in ("delete", "expire-delete"):
                    result = self._delete(op)
                elif op.action == "archive":
                    result = self._archive(op)
                elif op.action == "unarchive":
                    result = self._unarchive(op)
                else:
                    result = Result(op, False, f"unknown action {op.action}")
            except Exception as error:  # keep going; report this op as failed
                result = Result(op, False, f"{type(error).__name__}: {error}")
            if not result.skipped:
                self.log(op, "ok" if result.ok else "error", result.error, result.freed)
            else:
                self.log(op, "skipped", result.error)
            results.append(result)
        if results and not self.dry_run:
            self.store.save()  # deletions, archives and auto-unarchives all change the store
        return results

    # -- each action -------------------------------------------------------------------------------

    def _refusal(self, op: Op, part: Part) -> Optional[str]:
        reason = self.guard.check(part)
        if reason:
            return reason
        live = self.live()
        if live is not None and op.session_id and live.session_live(op.session_id):
            return "the session is live now"
        return None

    def _delete(self, op: Op) -> Result:
        """Check every part first, then delete; nothing is removed if any part is refused.

        Each part is measured again and compared with its fingerprint (from the scan, or from
        archiving). For an expired archive a changed part is still in use, so it is unarchived and
        the rest go ahead; for a plain delete it means the scan is stale, so the item is skipped.
        """
        expiring = op.action == "expire-delete"
        todo: List[Part] = []
        settled: List[str] = []      # already gone
        unarchived: List[str] = []   # changed since archiving
        refusal = ""
        for part in op.parts:
            if part.remover != "git-prune":
                current = measure(part.path)
                if current.missing:
                    settled.append(part.path)
                    continue
                if changed(part.fingerprint(), current):
                    if expiring:
                        unarchived.append(part.path)
                        continue
                    refusal = f"changed since the scan (rescan to see it): {part.path}"
                    break
            reason = self._refusal(op, part)
            if reason is None and part.remover == "git-worktree":
                reason = self.git.check_removable(part, op.force)
            if reason:
                refusal = f"{reason}: {part.path}"
                break
            todo.append(part)
        if not self.dry_run and (unarchived or (expiring and settled)):
            self.store.drop_paths(unarchived + (settled if expiring else []))
        if refusal:
            return Result(op, False, refusal, skipped=True)
        if not todo and unarchived:
            return Result(op, False, "changed since it was archived, so it was unarchived", skipped=True)

        freed, removed, error = 0, [], ""
        for part in todo:
            try:
                if not self.dry_run:
                    if part.remover == "fs":
                        remove_path(part.path)
                    elif part.remover == "git-worktree":
                        self.git.remove(part, op.force, op.delete_branch)
                    elif part.remover == "git-prune":
                        self.git.prune(part.repo)
            except Exception as exc:  # stop this item; what's already gone stays recorded as gone
                error = f"{type(exc).__name__}: {exc} ({part.path})"
                break
            freed += part.bytes
            removed.append(part.path)
        if not self.dry_run:
            self.store.drop_paths(removed + settled)
        if error:
            return Result(op, False, error, freed=freed)
        note = f"{len(unarchived)} part(s) changed since archiving and were unarchived" if unarchived else ""
        return Result(op, True, note, freed=freed)

    def _archive(self, op: Op) -> Result:
        for part in op.parts:
            reason = self.guard.check(part)
            if reason:
                return Result(op, False, f"{reason}: {part.path}", skipped=True)
        if not self.dry_run:
            self.store.drop_paths([p.path for p in op.parts])
            self.store.add(op.node_id, op.label, op.source, op.parts, self.env.now(), op.retention_days)
        return Result(op, True)

    def _unarchive(self, op: Op) -> Result:
        if not self.dry_run:
            self.store.drop_paths([p.path for p in op.parts])
        return Result(op, True)


def expired_plan(records: List[dict]) -> Plan:
    plan = Plan()
    for record in records:
        parts = [Part.from_dict(p) for p in record["parts"]]
        plan.ops.append(Op("expire-delete", record["id"], record["label"], record.get("source", ""), parts,
                           sum(p.bytes for p in parts)))
    return plan
