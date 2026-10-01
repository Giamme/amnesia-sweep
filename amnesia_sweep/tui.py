"""The curses shell around browser.py: key mapping, painting, and the scan/apply worker threads.

All decisions live in browser.py; this module only moves keys in, rows out, and runs the slow
work (scanning, deleting) off the UI thread so the screen keeps updating. Standard library only.
"""

from __future__ import annotations

import curses
import locale
import os
import queue
import threading
from typing import Dict, List, Optional

from . import scan
from .actions import Executor, expired_plan, log_events
from .archive import ArchiveStore, locked
from .browser import BrowserState, Effect, add_source, handle_key, remove_leaves, render
from .config import Config
from .env import Env
from .liveness import Liveness
from .model import Node, finalize
from .report import human_size
from .safety import guard_for
from .sizing import Meter
from .sources import all_sources

KEYS = {
    curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
    curses.KEY_PPAGE: "pgup", curses.KEY_NPAGE: "pgdn", curses.KEY_HOME: "home", curses.KEY_END: "end",
    curses.KEY_ENTER: "enter", 10: "enter", 13: "enter", curses.KEY_BACKSPACE: "backspace", 127: "backspace",
    8: "backspace", 27: "esc", curses.KEY_RESIZE: "resize", 32: "space",
}

STYLES = ("normal", "title", "crumb", "selected", "dim", "delete", "archive", "archived", "keep", "flag",
          "warn", "footer")


_ESCAPES = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
            "5~": "pgup", "6~": "pgdn", "1~": "home", "4~": "end", "3~": None}


def key_name(code: int) -> Optional[str]:
    if code in KEYS:
        return KEYS[code]
    if 32 < code < 127:
        return chr(code)
    return None


def read_key(screen) -> Optional[str]:
    """Read one key, decoding raw CSI/SS3 escape sequences that curses passed through as ESC."""
    code = screen.getch()
    if code == -1:
        return ""
    if code != 27:
        return key_name(code)
    screen.nodelay(True)
    try:
        follow = screen.getch()
        if follow == -1:
            return "esc"
        if follow not in (ord("["), ord("O")):
            return None  # Alt+key: ignore rather than treat as Esc
        tail = ""
        for _ in range(4):
            nxt = screen.getch()
            if nxt == -1:
                break
            tail += chr(nxt)
            if chr(nxt).isalpha() or chr(nxt) == "~":
                break
        return _ESCAPES.get(tail[-1:] if tail[-1:].isalpha() else tail)
    finally:
        screen.nodelay(False)
        screen.timeout(100)


def _clean(text: str) -> str:
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


class App:
    def __init__(self, env: Env, config: Config, store: ArchiveStore, events: List, no_color: bool = False,
                 pinned_retention: Optional[int] = None):
        self.env = env
        self.config = config
        self.store = store
        self.no_color = no_color or bool(os.environ.get("NO_COLOR"))
        self.queue: "queue.Queue" = queue.Queue()
        self.cancel = threading.Event()
        self.stop_apply = threading.Event()
        self.meters: Dict[str, Meter] = {}
        self.ctx = None
        self.sources: List = []
        self.attrs: Dict[str, int] = {}
        encoding = (locale.getpreferredencoding(False) or "").lower()
        root = Node(id="", label="All agents", kind="root")
        self.state = BrowserState(root=root, now=env.now(), retention_days=config.retention_days,
                                  unicode="utf" in encoding, pinned_retention=pinned_retention)
        for event in events:
            self.state.banner.append(("unarchived (changed since archiving): " if event.kind == "unarchived"
                                      else "no longer archived (gone): ") + event.path)
        self._load_expired()

    # -- setup ------------------------------------------------------------------------------------------

    def _load_expired(self) -> None:
        expired = self.store.expired(self.env.now(), self.config.retention_days)
        self.state.expired = expired
        if expired:
            self.state.screen = "expiry"
            self.state.expiry_checked = {r["id"] for r in expired}
            self.state.expiry_cursor = 0

    def _colors(self) -> None:
        plain = {"normal": curses.A_NORMAL, "title": curses.A_REVERSE | curses.A_BOLD, "crumb": curses.A_BOLD,
                 "selected": curses.A_REVERSE, "dim": curses.A_DIM, "delete": curses.A_BOLD,
                 "archive": curses.A_BOLD, "archived": curses.A_NORMAL, "keep": curses.A_NORMAL,
                 "flag": curses.A_DIM, "warn": curses.A_BOLD, "footer": curses.A_REVERSE}
        self.attrs = dict(plain)
        if self.no_color or not curses.has_colors():
            return
        try:
            curses.start_color()
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        pairs = {"title": (curses.COLOR_WHITE, curses.COLOR_BLUE), "delete": (curses.COLOR_RED, background),
                 "archive": (curses.COLOR_YELLOW, background), "archived": (curses.COLOR_BLUE, background),
                 "keep": (curses.COLOR_CYAN, background), "flag": (curses.COLOR_MAGENTA, background),
                 "warn": (curses.COLOR_YELLOW, background), "crumb": (curses.COLOR_CYAN, background)}
        for number, (style, (fg, bg)) in enumerate(pairs.items(), start=1):
            try:
                curses.init_pair(number, fg, bg)
                self.attrs[style] = curses.color_pair(number) | plain[style]
            except curses.error:
                pass

    # -- scanning ---------------------------------------------------------------------------------------

    def start_scan(self) -> None:
        self.cancel = threading.Event()
        self.ctx = scan.make_context(self.env, self.config, cancel=self.cancel)
        self.sources = scan.select_sources(self.ctx)
        self.meters = {s.name: Meter(self.cancel) for s in self.sources}
        self.state.order = [s.name for s in self.sources]
        self.state.root.children = []
        self.state.pending = {s.name: s.label for s in self.sources}
        finalize(self.state.root)
        self.state.reindex()
        sources, ctx, meters, out = list(self.sources), self.ctx, self.meters, self.queue

        def work() -> None:
            try:
                for _, node in scan.iter_scan(ctx, sources, meters):
                    out.put(("node", node))
            except Exception as error:  # surfaces in the footer instead of killing the UI
                out.put(("error", f"scan failed: {error}"))
            out.put(("scanned", None))

        threading.Thread(target=work, daemon=True).start()

    def drain(self) -> None:
        while True:
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "node":
                add_source(self.state, payload)
                scan.overlay_archive(self.state.root, self.store.records, self.config.retention_days)
            elif kind == "scanned":
                self.state.pending.clear()
                warnings = scan.enforce_invariants(self.state.root)
                finalize(self.state.root)
                scan.overlay_archive(self.state.root, self.store.records, self.config.retention_days)
                self.state.reindex()
                if warnings:
                    self.state.message = f"{len(warnings)} overlapping item(s) were made unremovable"
            elif kind == "error":
                self.state.message = payload
            elif kind == "applied":
                self._finish_apply(*payload)
        for name in list(self.state.pending):
            meter = self.meters.get(name)
            label = next((s.label for s in self.sources if s.name == name), name)
            if meter is not None:
                self.state.pending[name] = f"{label} — {meter.files:,} files, {human_size(meter.bytes)}"

    # -- applying -----------------------------------------------------------------------------------------

    def _executor(self) -> Executor:
        repos = set()
        for source in self.sources:
            repos |= set(getattr(source, "repos", ()) or ())
        guard = guard_for(self.env, all_sources(), self.ctx, repos)
        for record in self.store.records:
            guard.repos |= {os.path.realpath(p["repo"]) for p in record["parts"] if p.get("repo")}
        return Executor(self.env, guard, self.store, grace_minutes=self.config.active_grace_minutes,
                        probe_live=lambda: Liveness.probe(scan.registry_dirs(self.env)))

    def apply(self, plan, expiring: bool = False) -> None:
        self.stop_apply.clear()
        executor = self._executor()
        state, out, stop = self.state, self.queue, self.stop_apply

        def progress(number: int, total: int, op) -> None:
            state.progress = f"{number}/{total}  {op.action}  {human_size(op.bytes)}  {op.label}"

        def work() -> None:
            try:
                results = executor.run(plan, on_progress=progress, should_stop=stop.is_set)
            except Exception as error:
                results = []
                state.message = f"failed: {error}"
            out.put(("applied", (plan, results, expiring)))

        threading.Thread(target=work, daemon=True).start()

    def _finish_apply(self, plan, results, expiring: bool) -> None:
        state = self.state
        state.results = results
        state.banner = []
        deleted = {r.op.node_id for r in results if r.ok and r.op.action in ("delete", "expire-delete")}
        if deleted:
            remove_leaves(state, deleted)
        if expiring:
            gone = {r.op.node_id for r in results if r.ok}
            state.expired = [r for r in state.expired if r["id"] not in gone]
        state.marks.clear()
        state.plan = None
        scan.overlay_archive(state.root, self.store.records, self.config.retention_days)
        state.reindex()
        state.screen = "result"

    def handle(self, effect: Effect) -> bool:
        """Carry out an effect; return False to quit."""
        if effect.kind == "quit":
            return False
        if effect.kind == "rescan":
            self.cancel.set()
            self.start_scan()
        elif effect.kind == "apply" and effect.plan is not None:
            self.apply(effect.plan)
        elif effect.kind == "expire":
            chosen = [r for r in self.state.expired if r["id"] in set(effect.ids)]
            if chosen:
                self.apply(expired_plan(chosen), expiring=True)
            else:
                self.state.screen = "browse"
        elif effect.kind == "keep":
            for record_id in effect.ids:
                self.store.keep(record_id, self.env.now())
            self.store.save()
            self.state.expired = [r for r in self.state.expired if r["id"] not in set(effect.ids)]
            scan.overlay_archive(self.state.root, self.store.records, self.config.retention_days)
            self.state.message = f"kept {len(effect.ids)} item(s) for another {self.config.retention_days} days"
        return True

    # -- the loop -------------------------------------------------------------------------------------

    def paint(self, screen) -> None:
        height, width = screen.getmaxyx()
        rows = render(self.state, width, height)
        screen.erase()
        for y, row in enumerate(rows[:height]):
            x = 0
            for text, style in row:
                if x >= width:
                    break
                text = _clean(text)[: width - x]
                try:
                    screen.addstr(y, x, text, self.attrs.get(style, curses.A_NORMAL))
                except curses.error:
                    pass  # writing the bottom-right cell raises after drawing it
                x += len(text)
        screen.refresh()

    def main(self, screen) -> int:
        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(100)
        self._colors()
        self.start_scan()
        while True:
            try:
                self.drain()
                self.state.tick += 1
                self.state.now = self.env.now()
                self.paint(screen)
                name = read_key(screen)
                if name == "":
                    continue
                if name is None or name == "resize":
                    if name == "resize":
                        curses.update_lines_cols()
                    continue
                effect = handle_key(self.state, name)
                if effect is not None and not self.handle(effect):
                    self.cancel.set()
                    return 0
            except KeyboardInterrupt:
                if self.state.screen == "applying":
                    self.stop_apply.set()
                    self.state.progress += "   (stopping after this item…)"
                    continue
                self.cancel.set()
                return 130


def run(env: Env, config: Config, args) -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    from .cli import Fail

    pinned = getattr(args, "retention_days", None)
    with locked(env.state_dir):
        store = ArchiveStore(env.state_dir)
        try:
            store.load()
        except Exception as error:
            raise Fail(str(error))
        events = store.reconcile()
        if events:
            store.save()
            log_events(env.state_dir, events)
        app = App(env, config, store, events, no_color=getattr(args, "no_color", False), pinned_retention=pinned)
        return curses.wrapper(app.main)
