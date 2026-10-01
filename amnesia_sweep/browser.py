"""The interactive browser's state, key handling and layout, with no curses in sight.

tui.py feeds key names in and paints the rows this module renders; everything that decides what
a key does or what the screen shows lives here, so it can be tested without a terminal.
Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .actions import Plan, Result, build_plan
from .archive import expires_at, record_bytes
from .model import Node, finalize, iter_leaves, iter_nodes, parent_map, sort_children
from .report import flags_text, human_age, human_size

AGE_FILTERS: Tuple[Tuple[str, Optional[float]], ...] = (
    ("all ages", None), ("older than 7d", 7 * 86400), ("older than 30d", 30 * 86400),
    ("older than 90d", 90 * 86400))
SORTS = ("size", "age", "name")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MIN_WIDTH, MIN_HEIGHT = 60, 10

Segment = Tuple[str, str]   # (text, style name)
Row = List[Segment]


@dataclass
class Effect:
    kind: str                       # quit | rescan | apply | expire | keep
    plan: Optional[Plan] = None
    ids: List[str] = field(default_factory=list)


@dataclass
class BrowserState:
    root: Node
    now: float
    retention_days: int
    order: List[str] = field(default_factory=list)          # source names in display order
    stack: List[str] = field(default_factory=lambda: [""])  # node ids from the root to the current folder
    cursor: int = 0
    scroll: int = 0
    marks: Dict[str, str] = field(default_factory=dict)
    sort: str = "size"
    age_filter: int = 0
    hide_kept: bool = False
    screen: str = "browse"          # expiry | browse | confirm | applying | result | help | info | quit
    previous: str = "browse"
    pending: Dict[str, str] = field(default_factory=dict)  # source name -> progress text while scanning
    tick: int = 0
    message: str = ""
    banner: List[str] = field(default_factory=list)
    plan: Optional[Plan] = None
    force_dirty: bool = False
    delete_branches: bool = False
    pinned_retention: Optional[int] = None
    results: List[Result] = field(default_factory=list)
    progress: str = ""
    expired: List[dict] = field(default_factory=list)
    expiry_checked: Set[str] = field(default_factory=set)
    expiry_cursor: int = 0
    expiry_confirm: bool = False
    page: int = 20
    unicode: bool = True
    _index: Dict[str, Node] = field(default_factory=dict)
    _parents: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.reindex()

    # -- tree bookkeeping ---------------------------------------------------------------------------

    def reindex(self) -> None:
        self._index = {n.id: n for n in iter_nodes(self.root)}
        self._parents = parent_map(self.root)
        # Keep the path valid if the tree changed under it.
        while len(self.stack) > 1 and self.stack[-1] not in self._index:
            self.stack.pop()
        self.marks = {k: v for k, v in self.marks.items() if k in self._index}

    @property
    def current(self) -> Node:
        return self._index.get(self.stack[-1], self.root)

    def node(self, node_id: str) -> Optional[Node]:
        return self._index.get(node_id)

    def visible(self) -> List[Node]:
        nodes = sort_children(self.current.children, self.sort)
        cutoff = AGE_FILTERS[self.age_filter][1]
        if cutoff is not None:
            limit = self.now - cutoff
            nodes = [n for n in nodes if any(l.newest <= limit for l in iter_leaves(n))]
        if self.hide_kept:
            nodes = [n for n in nodes if n.actionable or n.archive is not None]
        return nodes

    def selected(self) -> Optional[Node]:
        rows = self.visible()
        if not rows:
            return None
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        return rows[self.cursor]

    # -- marks --------------------------------------------------------------------------------------

    def effective(self, node_id: str) -> Tuple[Optional[str], bool]:
        current: Optional[str] = node_id
        while current is not None:
            if current in self.marks:
                mark = self.marks[current]
                explicit = current == node_id
                return (None, explicit) if mark == "keep" else (mark, explicit)
            current = self._parents.get(current)
        return None, False

    def marked_totals(self) -> Dict[str, Tuple[int, int]]:
        """mark -> (leaf count, bytes) over actionable leaves, as a cheap preview of the plan."""
        totals: Dict[str, List[int]] = {}
        for leaf in iter_leaves(self.root):
            mark, _ = self.effective(leaf.id)
            if mark is None or (mark != "unarchive" and not leaf.actionable):
                continue
            if mark == "unarchive" and leaf.archive is None:
                continue
            entry = totals.setdefault(mark, [0, 0])
            entry[0] += 1
            entry[1] += leaf.bytes
        return {k: (v[0], v[1]) for k, v in totals.items()}

    def build(self) -> Plan:
        force = set(self.marks) if self.force_dirty else set()
        if self.force_dirty:
            force |= {n.id for n in iter_leaves(self.root) if "dirty" in n.flags}
        return build_plan(self.root, self.marks, retention_days=self.pinned_retention, force=force,
                          delete_branches=self.delete_branches)


# -- updates from the outside world ------------------------------------------------------------------


def add_source(state: BrowserState, node: Node) -> None:
    """Insert or replace one finished source, keeping the cursor on the same row."""
    from .scan import worth_showing

    keep = state.selected()
    keep_id = keep.id if keep else None
    state.root.children = [c for c in state.root.children if c.id != node.id]
    if worth_showing(node):
        state.root.children.append(node)
    rank = {name: i for i, name in enumerate(state.order)}
    state.root.children.sort(key=lambda n: rank.get(n.id, len(rank)))
    state.pending.pop(node.id, None)
    finalize(state.root)
    state.reindex()
    if keep_id is not None:
        for i, row in enumerate(state.visible()):
            if row.id == keep_id:
                state.cursor = i
                break


def remove_leaves(state: BrowserState, ids: Set[str]) -> None:
    """Drop deleted leaves (and groups left empty) and re-aggregate, without a rescan."""

    def prune(node: Node) -> bool:
        if node.id in ids:
            return False
        if node.children:
            node.children = [c for c in node.children if prune(c)]
            return bool(node.children) or node.kind in ("tool", "root")
        return True

    prune(state.root)
    finalize(state.root)
    state.reindex()


# -- key handling -------------------------------------------------------------------------------------


def handle_key(state: BrowserState, key: str) -> Optional[Effect]:
    state.message = ""
    handler = {"browse": _browse_key, "confirm": _confirm_key, "expiry": _expiry_key, "help": _back_key,
               "info": _back_key, "result": _back_key, "quit": _quit_key, "applying": _ignore_key}
    return handler.get(state.screen, _back_key)(state, key)


def _ignore_key(state: BrowserState, key: str) -> Optional[Effect]:
    return None


def _back_key(state: BrowserState, key: str) -> Optional[Effect]:
    state.screen = "browse"
    return None


def _quit_key(state: BrowserState, key: str) -> Optional[Effect]:
    if key == "y":
        return Effect("quit")
    state.screen = "browse"
    return None


def _browse_key(state: BrowserState, key: str) -> Optional[Effect]:
    rows = state.visible()
    count = len(rows)
    if key in ("up", "k"):
        state.cursor = max(0, state.cursor - 1)
    elif key in ("down", "j"):
        state.cursor = min(max(0, count - 1), state.cursor + 1)
    elif key == "pgup":
        state.cursor = max(0, state.cursor - state.page)
    elif key == "pgdn":
        state.cursor = min(max(0, count - 1), state.cursor + state.page)
    elif key in ("home", "g"):
        state.cursor = 0
    elif key in ("end", "G"):
        state.cursor = max(0, count - 1)
    elif key in ("right", "enter", "l"):
        node = state.selected()
        if node is not None and node.children:
            state.stack.append(node.id)
            state.cursor = state.scroll = 0
        elif node is not None:
            state.screen, state.previous = "info", "browse"
    elif key in ("left", "backspace", "h"):
        if len(state.stack) > 1:
            left = state.stack.pop()
            rows = state.visible()
            state.cursor = next((i for i, n in enumerate(rows) if n.id == left), 0)
            state.scroll = max(0, state.cursor - state.page // 2)
    elif key in ("d", "a"):
        _toggle(state, "delete" if key == "d" else "archive")
    elif key == "u":
        node = state.selected()
        if node is not None:
            state.marks.pop(node.id, None)
            if state.effective(node.id)[0] is not None:
                state.marks[node.id] = "keep"
    elif key == "U":
        state.marks.clear()
        state.message = "cleared all marks"
    elif key == "s":
        state.sort = SORTS[(SORTS.index(state.sort) + 1) % len(SORTS)]
        state.message = f"sorted by {state.sort}"
    elif key == "o":
        state.age_filter = (state.age_filter + 1) % len(AGE_FILTERS)
        state.cursor = 0
        state.message = f"showing {AGE_FILTERS[state.age_filter][0]}"
    elif key == ".":
        state.hide_kept = not state.hide_kept
        state.cursor = 0
        state.message = "hiding items that can't be removed" if state.hide_kept else "showing everything"
    elif key == "i":
        if state.selected() is not None:
            state.screen = "info"
    elif key == "?":
        state.screen = "help"
    elif key == "r":
        if state.marks:
            state.message = "clear your marks (U) before rescanning"
        else:
            return Effect("rescan")
    elif key == "x":
        if state.pending:
            state.message = "still scanning; wait for it to finish before applying"
        elif not state.marks:
            state.message = "nothing marked: d marks for deletion, a for archiving"
        else:
            state.plan = state.build()
            state.scroll = 0
            state.screen = "confirm"
    elif key in ("q", "esc"):
        if state.marks:
            state.screen = "quit"
        else:
            return Effect("quit")
    return None


def _toggle(state: BrowserState, mark: str) -> None:
    node = state.selected()
    if node is None:
        return
    if node.id in state.pending:
        state.message = "this source is still being scanned"
        return
    if mark == "archive" and node.archive is not None and not node.archive.partial:
        mark = "unarchive"
    current, explicit = state.effective(node.id)
    if current == mark:
        if explicit:
            del state.marks[node.id]
        else:
            state.marks[node.id] = "keep"
        return
    if mark != "unarchive" and not node.actionable:
        state.message = f"can't {mark} this: {node.reason or 'not removable'}"
        return
    state.marks[node.id] = mark
    if node.children:  # a fresh group mark replaces whatever was marked inside it
        for each in iter_nodes(node):
            if each is not node:
                state.marks.pop(each.id, None)
    rows = state.visible()
    state.cursor = min(len(rows) - 1, state.cursor + 1)


def _confirm_key(state: BrowserState, key: str) -> Optional[Effect]:
    if key == "y" and state.plan is not None and state.plan.ops:
        state.screen = "applying"
        return Effect("apply", plan=state.plan)
    if key == "f":
        state.force_dirty = not state.force_dirty
        state.plan = state.build()
    elif key == "b":
        state.delete_branches = not state.delete_branches
        state.plan = state.build()
    elif key in ("up", "k"):
        state.scroll = max(0, state.scroll - 1)
    elif key in ("down", "j"):
        state.scroll += 1
    elif key == "pgdn":
        state.scroll += state.page
    elif key == "pgup":
        state.scroll = max(0, state.scroll - state.page)
    elif key in ("n", "esc", "q", "left", "backspace"):
        state.screen = "browse"
    return None


def _expiry_key(state: BrowserState, key: str) -> Optional[Effect]:
    ids = [r["id"] for r in state.expired]
    if state.expiry_confirm:
        state.expiry_confirm = False
        if key == "y":
            chosen = [i for i in ids if i in state.expiry_checked]
            state.screen = "applying"
            return Effect("expire", ids=chosen)
        return None
    if key == "up":  # "k" means keep on this screen
        state.expiry_cursor = max(0, state.expiry_cursor - 1)
    elif key in ("down", "j"):
        state.expiry_cursor = min(len(ids) - 1, state.expiry_cursor + 1)
    elif key in (" ", "space") and ids:
        record_id = ids[state.expiry_cursor]
        state.expiry_checked.symmetric_difference_update({record_id})
    elif key == "A":
        state.expiry_checked = set(ids) if len(state.expiry_checked) < len(ids) else set()
    elif key in ("enter", "d"):
        if state.expiry_checked:
            state.expiry_confirm = True
        else:
            state.message = "nothing checked (space checks an item)"
    elif key == "k":
        chosen = [i for i in ids if i in state.expiry_checked] or ids
        state.screen = "browse"
        return Effect("keep", ids=chosen)
    elif key in ("s", "esc", "q", "n"):
        state.screen = "browse"
        state.message = "skipped; expired items will be offered again next run"
    return None


# -- rendering -------------------------------------------------------------------------------------------


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[:max(0, width - 1)] + "…"


def _pad(text: str, width: int) -> str:
    return _fit(text, width).ljust(width)


def _crumb(state: BrowserState) -> str:
    names = [state.node(i).label if state.node(i) else "?" for i in state.stack[1:]]
    return " › ".join(["All agents", *names])


def _bar(fraction: float, width: int, unicode: bool) -> str:
    filled = max(0, min(width, int(round(fraction * width))))
    full, empty = ("█", "░") if unicode else ("#", ".")
    return full * filled + empty * (width - filled)


def _mark_cell(state: BrowserState, node: Node) -> Tuple[str, str]:
    mark, explicit = state.effective(node.id)
    if node.id in state.marks and state.marks[node.id] == "keep":
        return "K", "keep"
    if mark is None:
        if node.archive is not None:
            return ("a" if node.archive.partial else "A"), "archived"
        return " ", "normal"
    letter = {"delete": "d", "archive": "a", "unarchive": "u"}[mark]
    if mark != "unarchive" and not node.actionable:
        return "-", "dim"
    return (letter.upper() if explicit else letter), ("delete" if mark == "delete" else "archive")


def render(state: BrowserState, width: int, height: int) -> List[Row]:
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return [[(_fit(f"terminal too small ({width}x{height}); need {MIN_WIDTH}x{MIN_HEIGHT}", width), "warn")]]
    state.page = max(1, height - 6)
    screen = {"expiry": _render_expiry, "confirm": _render_confirm, "help": _render_help,
              "info": _render_info, "result": _render_result, "applying": _render_applying}.get(state.screen)
    rows = screen(state, width, height) if screen else _render_browse(state, width, height)
    if state.screen == "quit":
        rows[-1] = [(_pad(f"Discard {len(state.marks)} mark(s) and quit? y/n", width), "warn")]
    return rows[:height] + [[("", "normal")]] * max(0, height - len(rows))


def _header(state: BrowserState, width: int) -> Row:
    root = state.root
    text = f" amnesia-sweep   total {human_size(root.bytes)} · reclaimable {human_size(root.reclaimable)}"
    if state.pending:
        frame = SPINNER[state.tick % len(SPINNER)] if state.unicode else "|/-\\"[state.tick % 4]
        text += f"   {frame} scanning {len(state.pending)} source(s)"
    return [(_pad(text, width), "title")]


def _footer(state: BrowserState, width: int) -> Row:
    if state.message:
        return [(_pad(" " + state.message, width), "warn")]
    totals = state.marked_totals()
    bits = []
    for mark, verb in (("delete", "delete"), ("archive", "archive"), ("unarchive", "unarchive")):
        if mark in totals:
            count, nbytes = totals[mark]
            bits.append(f"{verb} {count} ({human_size(nbytes)})")
    hints = "d delete · a archive · u unmark · x apply · ? help · q quit"
    text = (" " + " · ".join(bits) + "   " + hints) if bits else " " + hints
    return [(_pad(text, width), "footer")]


def _render_browse(state: BrowserState, width: int, height: int) -> List[Row]:
    rows: List[Row] = [_header(state, width)]
    current = state.current
    crumb = f" {_crumb(state)}  ({human_size(current.bytes)})"
    filt = AGE_FILTERS[state.age_filter][0]
    extras = [f"sort: {state.sort}"] + ([filt] if state.age_filter else []) + (["hiding kept"] if state.hide_kept else [])
    rows.append([(_pad(crumb, width - 2 - len(" · ".join(extras))), "crumb"),
                 (" · ".join(extras) + "  ", "dim")])
    nodes = state.visible()
    body = height - 4
    if nodes:
        state.cursor = max(0, min(state.cursor, len(nodes) - 1))
    if state.cursor < state.scroll:
        state.scroll = state.cursor
    elif state.cursor >= state.scroll + body:
        state.scroll = state.cursor - body + 1
    biggest = max((n.bytes for n in nodes), default=0) or 1
    bar_width = 10 if width >= 90 else 6
    for i, node in enumerate(nodes[state.scroll:state.scroll + body], start=state.scroll):
        mark, mark_style = _mark_cell(state, node)
        flags = flags_text(node, state.now)
        if node.id in state.pending:
            flags = "scanning…"
        age = human_age(state.now - node.newest) if node.newest else "-"
        lead = f" {mark} {human_size(node.bytes):>10} {_bar(node.bytes / biggest, bar_width, state.unicode)} {age:>5}  "
        label = node.label + ("/" if node.children else "")
        if width - len(lead) - len(flags) - 2 < 16:
            flags = ""
        room = width - len(lead) - len(flags) - 2
        style = "selected" if i == state.cursor else ("dim" if not node.actionable and node.archive is None else "normal")
        row: Row = [(f" {mark} ", mark_style if i != state.cursor else "selected"),
                    (lead[3:], style), (_pad(label, max(1, room)), style), (f" {flags} ", "flag" if i != state.cursor else "selected")]
        rows.append(row)
    if current is state.root:
        for name, progress in sorted(state.pending.items()):
            if len(rows) >= height - 2:
                break
            frame = SPINNER[state.tick % len(SPINNER)] if state.unicode else "*"
            rows.append([(_pad(f"   {frame} {progress}", width), "dim")])
    if not nodes and not (current is state.root and state.pending):
        rows.append([(_pad("   (nothing here" + (" matches the age filter" if state.age_filter else "") + ")", width), "dim")])
    while len(rows) < height - 2:
        rows.append([("", "normal")])
    detail = ""
    node = state.selected()
    if node is not None:
        if not node.actionable and node.reason:
            detail = f" kept: {node.reason}"
        elif node.archive is not None:
            left = node.archive.days_left(state.now)
            detail = (f" archived; offered for deletion in {left:.0f} days" if left > 0
                      else " archived; its time is up, it'll be offered for deletion")
        elif node.meta.get("path"):
            detail = " " + node.meta["path"]
        elif node.meta.get("cwd"):
            detail = " " + node.meta["cwd"]
        if node.meta.get("warning"):
            detail += f"  ({node.meta['warning']})"
    rows.append([(_pad(detail, width), "dim")])
    rows.append(_footer(state, width))
    return rows


def _render_confirm(state: BrowserState, width: int, height: int) -> List[Row]:
    plan = state.plan or Plan()
    lines: List[Row] = []

    def section(title: str, ops, style: str) -> None:
        if not ops:
            return
        lines.append([(_pad(title, width), style)])
        for op in ops:
            notes = []
            if "dirty" in op.flags:
                notes.append("uncommitted changes: " + ("FORCED" if op.force else "will be refused (f to force)"))
            if "unmerged" in op.flags:
                notes.append("unmerged commits; the branch is kept" + ("" if not op.delete_branch else " (not merged, so -d skips it)"))
            note = f"  [{'; '.join(notes)}]" if notes else ""
            lines.append([(_pad(f"   {human_size(op.bytes):>10}  {op.label}{note}", width), "normal")])
        lines.append([("", "normal")])

    deletes = plan.of("delete")
    archives = plan.of("archive")
    days = state.pinned_retention or state.retention_days
    section(f" Permanently delete {len(deletes)} item(s), {human_size(plan.total('delete'))}", deletes, "delete")
    section(f" Archive {len(archives)} item(s), {human_size(plan.total('archive'))} — nothing is moved; "
            f"offered for deletion in {days} days", archives, "archive")
    section(f" Unarchive {len(plan.of('unarchive'))} item(s)", plan.of("unarchive"), "archive")
    skipped = plan.skipped_by_reason()
    if skipped:
        lines.append([(_pad(" Left alone", width), "dim")])
        for reason, count, nbytes in skipped:
            lines.append([(_pad(f"   {count} item(s), {human_size(nbytes)}: {reason}", width), "dim")])
    if not plan.ops:
        lines.append([(_pad(" Nothing in the marked items can be changed.", width), "warn")])
    body = height - 3
    state.scroll = max(0, min(state.scroll, max(0, len(lines) - body)))
    rows: List[Row] = [[(_pad(" Review — nothing has happened yet", width), "title")]]
    rows += lines[state.scroll:state.scroll + body]
    while len(rows) < height - 1:
        rows.append([("", "normal")])
    toggles = (f"f force dirty worktrees: {'on' if state.force_dirty else 'off'} · "
               f"b delete merged branches: {'on' if state.delete_branches else 'off'}")
    action = " y apply · n back · " if plan.ops else " n back · "
    rows.append([(_pad(action + toggles, width), "footer")])
    return rows


def _render_expiry(state: BrowserState, width: int, height: int) -> List[Row]:
    total = sum(record_bytes(r) for r in state.expired)
    rows: List[Row] = [[(_pad(f" {len(state.expired)} archived item(s) are past their retention period "
                              f"({human_size(total)}). Delete them?", width), "title")]]
    for line in state.banner:
        rows.append([(_pad(" " + line, width), "warn")])
    rows.append([("", "normal")])
    body = height - len(rows) - 2
    start = max(0, state.expiry_cursor - body + 1)
    for i, record in enumerate(state.expired[start:start + body], start=start):
        box = "[x]" if record["id"] in state.expiry_checked else "[ ]"
        ago = human_age(state.now - expires_at(record, state.retention_days))
        text = f" {box} {human_size(record_bytes(record)):>10}  {record['label']}  (due {ago} ago)"
        rows.append([(_pad(text, width), "selected" if i == state.expiry_cursor else "normal")])
    while len(rows) < height - 1:
        rows.append([("", "normal")])
    if state.expiry_confirm:
        checked = [r for r in state.expired if r["id"] in state.expiry_checked]
        rows.append([(_pad(f" Permanently delete {len(checked)} item(s), "
                           f"{human_size(sum(record_bytes(r) for r in checked))}? y/n", width), "warn")])
    elif state.message:
        rows.append([(_pad(" " + state.message, width), "warn")])
    else:
        rows.append([(_pad(f" space check · A all · enter delete checked · k keep {state.retention_days} more days"
                           " · s skip for now", width), "footer")])
    return rows


def _render_info(state: BrowserState, width: int, height: int) -> List[Row]:
    node = state.selected()
    rows: List[Row] = [[(_pad(" Details", width), "title")]]
    if node is None:
        return rows
    lines = [f"Item      {node.label}", f"Id        {node.id}", f"Size      {human_size(node.bytes)} in {node.files} file(s)",
             f"Changed   {human_age(state.now - node.newest)} ago" if node.newest else "Changed   -",
             f"Kind      {node.kind} ({node.risk})",
             f"Removable {'yes' if node.actionable else 'no: ' + (node.reason or '')}"]
    flags = flags_text(node, state.now)
    if flags:
        lines.append(f"Flags     {flags}")
    if node.archive is not None:
        left = node.archive.days_left(state.now)
        lines.append(f"Archive   {'partly ' if node.archive.partial else ''}archived; "
                     + (f"{left:.0f} days left" if left > 0 else "due for deletion")
                     + (" (pinned)" if node.archive.pinned else ""))
    for key, value in sorted(node.meta.items()):
        if value and key not in ("path",):
            lines.append(f"{key:<9} {value}")
    paths = [p.path for p in node.parts] or ([node.meta["path"]] if node.meta.get("path") else [])
    if paths:
        lines.append("Paths")
        lines += [f"  {p}" for p in paths[: max(1, height - len(lines) - 4)]]
    if node.children:
        leaves = list(iter_leaves(node))
        lines.append(f"Contains  {len(leaves)} item(s), {sum(1 for l in leaves if l.actionable)} removable")
    rows += [[(_pad(" " + line, width), "normal")] for line in lines]
    while len(rows) < height - 1:
        rows.append([("", "normal")])
    rows.append([(_pad(" any key to go back", width), "footer")])
    return rows


HELP = (
    "Move        ↑ ↓ (or j k), PgUp PgDn, g G",
    "Open        → Enter l      Back  ← Backspace h",
    "Delete      d  mark for permanent deletion (on a folder: everything removable inside)",
    "Archive     a  mark to archive: nothing moves; after the retention period you'll be",
    "               asked whether to delete it. On an archived item, a unarchives it.",
    "Unmark      u  (inside a marked folder this keeps just this item)    U clears all",
    "Apply       x  review everything marked, then y to go ahead",
    "Sort        s  size → age → name      Age filter  o      Hide kept items  .",
    "Details     i                         Rescan  r           Quit  q",
    "",
    "Marks: D/A/U set here, d/a/u inherited from a folder, K kept, - can't be removed.",
    "Flags: LIVE in use now · ORPHAN its project folder is gone · DIRTY uncommitted",
    "changes · UNMERGED commits not on the main branch · CURRENT version in use ·",
    "A 12d archived, 12 days left · KEPT never touched (config, credentials, memory).",
)


def _render_help(state: BrowserState, width: int, height: int) -> List[Row]:
    rows: List[Row] = [[(_pad(" Keys", width), "title")]]
    rows += [[(_pad("  " + line, width), "normal")] for line in HELP]
    while len(rows) < height - 1:
        rows.append([("", "normal")])
    rows.append([(_pad(" any key to go back", width), "footer")])
    return rows


def _render_applying(state: BrowserState, width: int, height: int) -> List[Row]:
    rows: List[Row] = [[(_pad(" Working…", width), "title")], [("", "normal")],
                       [(_pad("  " + state.progress, width), "normal")]]
    while len(rows) < height - 1:
        rows.append([("", "normal")])
    rows.append([(_pad(" Ctrl-C stops after the current item", width), "footer")])
    return rows


def _render_result(state: BrowserState, width: int, height: int) -> List[Row]:
    results = state.results
    freed = sum(r.freed for r in results)
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    rows: List[Row] = [[(_pad(f" Done: {len(ok)} item(s)" + (f", {human_size(freed)} freed" if freed else ""), width),
                         "title")], [("", "normal")]]
    for result in failed:
        rows.append([(_pad(f"  FAILED  {result.op.label}: {result.error}", width), "delete")])
    for result in skipped:
        rows.append([(_pad(f"  skipped {result.op.label}: {result.error}", width), "dim")])
    for line in state.banner:
        rows.append([(_pad("  " + line, width), "warn")])
    while len(rows) < height - 1:
        rows.append([("", "normal")])
    rows.append([(_pad(" any key to continue", width), "footer")])
    return rows
