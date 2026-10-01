"""Command-line entry points for amnesia-sweep.

Exit codes: 0 ok, 1 runtime failure (a delete failed, unreadable state, another run holds the
lock), 2 usage error, 3 nothing found / nothing to do, 130 interrupted. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from . import config as config_mod
from .archive import ArchiveError, ArchiveStore, LockHeld, effective_retention, expires_at, locked, record_bytes
from .env import Env, iso
from .report import human_age, human_size, node_json, parse_age, parse_size, render_text, totals


class _ArgumentParser(argparse.ArgumentParser):
    """An argparse parser that reports errors without terminating the process."""

    def exit(self, status=0, message=None):
        if message:
            self._print_message(message, sys.stderr)
        raise _ArgumentExit(status)


class _ArgumentExit(Exception):
    def __init__(self, status):
        self.status = status


class Fail(Exception):
    def __init__(self, message: str, status: int = 1):
        super().__init__(message)
        self.status = status


def _common(suppress: bool) -> argparse.ArgumentParser:
    """Global flags, accepted before or after the subcommand."""
    default = argparse.SUPPRESS if suppress else None
    flags = _ArgumentParser(add_help=False)
    flags.add_argument("--dry-run", action="store_true", default=default if suppress else False,
                       help="show what would happen; change nothing")
    flags.add_argument("--include-models", action="store_true", default=default if suppress else False,
                       help="make downloaded local models actionable")
    flags.add_argument("--include-user-content", action="store_true", default=default if suppress else False,
                       help="make Claude plans and prompt history actionable")
    flags.add_argument("--all-worktrees", action="store_true", default=default if suppress else False,
                       help="list git worktrees that don't look agent-made too")
    flags.add_argument("--retention-days", type=int, default=default,
                       help="days an archived item is kept before it's offered for deletion")
    flags.add_argument("--no-color", action="store_true", default=default if suppress else False)
    return flags


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="amnesia-sweep", parents=[_common(False)],
                             description="See what AI coding agents left on disk, then delete it or archive it.")
    parser.add_argument("--version", action="version", version=f"amnesia-sweep {__version__}")
    commands = parser.add_subparsers(dest="command", parser_class=_ArgumentParser)
    sub = _common(True)

    commands.add_parser("tui", parents=[sub], help="interactive browser (the default on a terminal)")

    scan = commands.add_parser("scan", parents=[sub], help="print the overview")
    scan.add_argument("--json", action="store_true", dest="as_json")
    scan.add_argument("--depth", type=int, default=None, help="levels to show (text default 2)")
    scan.add_argument("--min-size", default="0", help="hide rows smaller than this, e.g. 10M")
    scan.add_argument("--older-than", default=None, help="only count items untouched for this long, e.g. 30d")
    scan.add_argument("--sort", choices=("size", "age", "name"), default="size")
    scan.add_argument("--source", default=None, help="comma-separated source names, e.g. claude,codex")
    scan.add_argument("--ids", action="store_true", help="show item ids (use them with delete/archive)")
    scan.add_argument("--top", type=int, default=10, help="how many largest reclaimable items to list")

    delete = commands.add_parser("delete", parents=[sub], help="permanently delete items by id or path")
    delete.add_argument("targets", nargs="+")
    delete.add_argument("--yes", action="store_true", help="don't ask")
    delete.add_argument("--force-dirty", action="store_true", help="also remove worktrees with uncommitted changes")
    delete.add_argument("--delete-merged-branch", action="store_true",
                        help="after removing a worktree, delete its branch if it's merged")

    archive = commands.add_parser("archive", parents=[sub], help="mark items to delete later, or manage marks")
    archive_cmds = archive.add_subparsers(dest="archive_command", parser_class=_ArgumentParser)
    add = archive_cmds.add_parser("add", parents=[sub], help="archive items by id or path")
    add.add_argument("targets", nargs="+")
    listing = archive_cmds.add_parser("list", parents=[sub], help="list archived items")
    listing.add_argument("--json", action="store_true", dest="as_json")
    listing.add_argument("--expired", action="store_true", help="only items past their retention period")
    remove = archive_cmds.add_parser("remove", parents=[sub], help="unarchive items")
    remove.add_argument("targets", nargs="+")

    sweep = commands.add_parser("sweep", parents=[sub], help="delete archived items whose time is up (asks first)")
    sweep.add_argument("--yes", action="store_true", help="delete without asking (for cron)")

    cfg = commands.add_parser("config", parents=[sub], help="show or change settings")
    cfg_cmds = cfg.add_subparsers(dest="config_command", parser_class=_ArgumentParser)
    get = cfg_cmds.add_parser("get")
    get.add_argument("key", nargs="?")
    setter = cfg_cmds.add_parser("set")
    setter.add_argument("key")
    setter.add_argument("value")
    cfg_cmds.add_parser("path")

    log = commands.add_parser("log", parents=[sub], help="show what amnesia-sweep has done")
    log.add_argument("-n", type=int, default=50)
    log.add_argument("--json", action="store_true", dest="as_json")
    return parser


# -- shared setup -------------------------------------------------------------------------------


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _quiet_broken_pipe() -> None:
    """Make interpreter shutdown harmless after stdout's pipe is closed."""
    try:
        stdout_fd = sys.stdout.fileno()
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, stdout_fd)
        finally:
            os.close(devnull_fd)
    except (OSError, AttributeError, ValueError):
        pass


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _config(env: Env, args) -> config_mod.Config:
    try:
        cfg = config_mod.load(env.config_dir)
    except config_mod.ConfigError as error:
        raise Fail(str(error))
    if getattr(args, "include_models", False):
        cfg.include_models = True
    if getattr(args, "include_user_content", False):
        cfg.include_user_content = True
    if getattr(args, "all_worktrees", False):
        cfg.all_worktrees = True
    if getattr(args, "retention_days", None) is not None:
        if args.retention_days < 1:
            raise Fail("--retention-days must be at least 1", 2)
        cfg.retention_days = args.retention_days
    return cfg


def _store(env: Env) -> ArchiveStore:
    try:
        return ArchiveStore(env.state_dir).load()
    except ArchiveError as error:
        raise Fail(str(error))


def _reconcile(store: ArchiveStore, dry_run: bool, quiet: bool = False) -> list:
    from . import actions

    events = store.reconcile()
    if events and not dry_run:
        store.save()
        actions.log_events(store.state_dir, events)
    if not quiet:
        for event in events:
            if event.kind == "unarchived":
                _err(f"unarchived (changed since archiving, so still in use): {event.path}")
            else:
                _err(f"no longer archived (it's gone): {event.path}")
    return events


def _reconciled_store(env: Env, dry_run: bool, quiet: bool = False) -> ArchiveStore:
    """Load the archive and reconcile it, saving only while holding the lock.

    Read-only commands use this: if another run holds the lock, they still show current
    results but leave the state file for that run to update.
    """
    try:
        with locked(env.state_dir):
            store = _store(env)
            _reconcile(store, dry_run, quiet)
            return store
    except LockHeld:
        store = _store(env)
        _reconcile(store, True, quiet=True)
        return store


def _scan(env: Env, cfg, names: Optional[List[str]] = None):
    from . import scan

    ctx = scan.make_context(env, cfg)
    sources = scan.select_sources(ctx, names)
    root = scan.scan_all(ctx, sources)
    return ctx, sources, root


# -- commands -------------------------------------------------------------------------------------


def cmd_scan(env: Env, args) -> int:
    from . import scan

    cfg = _config(env, args)
    try:
        min_size = parse_size(args.min_size)
        older = parse_age(args.older_than) if args.older_than else None
    except ValueError as error:
        raise Fail(str(error), 2)
    names = [n.strip() for n in args.source.split(",")] if args.source else None
    store = _reconciled_store(env, args.dry_run, quiet=args.as_json)
    ctx, sources, root = _scan(env, cfg, names)
    if not root.children:
        _err("no agent data found" + (f" for {args.source}" if args.source else ""))
        return 3
    scan.overlay_archive(root, store.records, cfg.retention_days)
    if older is not None:
        root = scan.filter_older_than(root, older, env.now())
    now = env.now()
    expired = store.expired(now, cfg.retention_days)
    warnings = [w for w in root.meta.get("warnings", "").split("\n") if w]
    if args.as_json:
        payload = {"version": 1, "generated_at": iso(now), "home": env.home, "totals": totals(root),
                   "expired": [_record_json(r, cfg.retention_days, now) for r in expired],
                   "warnings": warnings, "nodes": [node_json(c, now, args.depth) for c in root.children]}
        _out(json.dumps(payload, ensure_ascii=False, indent=None))
    else:
        _out(render_text(root, now, depth=args.depth or 2, min_size=min_size, show_ids=args.ids,
                         sort=args.sort, top=args.top, warnings=warnings))
        if expired:
            _err(f"\n{len(expired)} archived item(s) are past their retention period "
                 f"({human_size(sum(record_bytes(r) for r in expired))}); run `amnesia-sweep sweep` to review them")
    sys.stdout.flush()
    return 0


def _record_json(record: dict, default_days: int, now: float) -> dict:
    return {"id": record["id"], "label": record["label"], "source": record.get("source", ""),
            "archived_at": record["archived_at"], "retention_days": effective_retention(record, default_days),
            "pinned": record.get("retention_days") is not None,
            "expires_at": iso(expires_at(record, default_days)),
            "days_left": round((expires_at(record, default_days) - now) / 86400, 2),
            "bytes": record_bytes(record), "paths": [p["path"] for p in record["parts"]]}


def _guard(env: Env, ctx, scanned_sources=()):
    from .safety import guard_for
    from .sources import all_sources

    repos = set()
    for source in scanned_sources:
        repos |= set(getattr(source, "repos", ()) or ())
    return guard_for(env, all_sources(), ctx, repos)


def _executor(env: Env, ctx, store: ArchiveStore, guard, dry_run: bool):
    from . import scan
    from .actions import Executor
    from .liveness import Liveness

    return Executor(env, guard, store, dry_run=dry_run, grace_minutes=ctx.config.active_grace_minutes,
                    probe_live=lambda: Liveness.probe(scan.registry_dirs(env)))


def _resolve(root, targets: List[str]) -> list:
    from .model import resolve_target

    nodes, missing = [], []
    for target in targets:
        hits = resolve_target(root, target)
        if not hits:
            missing.append(target)
        elif len(hits) > 1:
            listing = "\n".join(f"  {h.id}" for h in hits[:20])
            more = f"\n  … and {len(hits) - 20} more" if len(hits) > 20 else ""
            raise Fail(f"{target!r} matches {len(hits)} items; use one of these ids:\n{listing}{more}", 2)
        else:
            nodes.append(hits[0])
    if missing:
        raise Fail(f"nothing matches {', '.join(repr(m) for m in missing)} "
                   "(see `amnesia-sweep scan --ids --depth 5`)", 3)
    return nodes


def _print_plan(plan, action: str, verb: str, limit: int = 30) -> None:
    ops = plan.of(action)
    for op in ops[:limit]:
        notes = []
        if "dirty" in op.flags:
            notes.append("has uncommitted changes" + ("; forced" if op.force else "; will be refused"))
        if "unmerged" in op.flags:
            notes.append("branch has unmerged commits (the branch is kept)")
        suffix = f"  ({'; '.join(notes)})" if notes else ""
        _out(f"  {verb} {human_size(op.bytes):>10}  {op.label}{suffix}")
    if len(ops) > limit:
        _out(f"  … and {len(ops) - limit} more ({human_size(sum(o.bytes for o in ops[limit:]))})")
    for reason, count, nbytes in plan.skipped_by_reason():
        _out(f"  skip {count} item(s), {human_size(nbytes)}: {reason}")


def _report_results(results) -> int:
    failed = [r for r in results if not r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    freed = sum(r.freed for r in results)
    done = sum(1 for r in results if r.ok)
    _out(f"done: {done} item(s), {human_size(freed)} freed" if freed else f"done: {done} item(s)")
    for result in skipped:
        _out(f"  skipped {result.op.label}: {result.error}")
    for result in failed:
        _out(f"  FAILED  {result.op.label}: {result.error}")
    return 1 if failed else 0


def cmd_delete(env: Env, args) -> int:
    from .actions import build_plan

    cfg = _config(env, args)
    with locked(env.state_dir):
        store = _store(env)
        _reconcile(store, args.dry_run)
        ctx, sources, root = _scan(env, cfg)
        from . import scan

        scan.overlay_archive(root, store.records, cfg.retention_days)
        nodes = _resolve(root, args.targets)
        marks = {n.id: "delete" for n in nodes}
        plan = build_plan(root, marks, force=marks if args.force_dirty else (),
                          delete_branches=args.delete_merged_branch)
        if not plan.of("delete"):
            _print_plan(plan, "delete", "delete")
            _err("nothing here can be deleted")
            return 3
        total = plan.total("delete")
        _out(f"{'Would permanently delete' if args.dry_run else 'Permanently delete'} "
             f"{len(plan.of('delete'))} item(s), {human_size(total)}:")
        _print_plan(plan, "delete", "delete")
        if not args.dry_run and not args.yes:
            if not _is_tty():
                raise Fail("not on a terminal; pass --yes to delete without asking", 2)
            from .prompt import confirm

            if not confirm("Delete these permanently?"):
                _out("nothing deleted")
                return 0
        executor = _executor(env, ctx, store, _guard(env, ctx, sources), args.dry_run)
        results = executor.run(plan)
        if args.dry_run:
            refused = [r for r in results if not r.ok]
            for result in refused:
                _out(f"  would refuse {result.op.label}: {result.error}")
            return 0
        return _report_results(results)


def cmd_archive(env: Env, args) -> int:
    sub = getattr(args, "archive_command", None)
    if sub == "list":
        return _archive_list(env, args)
    if sub == "remove":
        return _archive_remove(env, args)
    if sub != "add":
        raise Fail("use `archive add`, `archive list` or `archive remove`", 2)
    from . import scan
    from .actions import build_plan

    cfg = _config(env, args)
    pinned = args.retention_days if getattr(args, "retention_days", None) is not None else None
    with locked(env.state_dir):
        store = _store(env)
        _reconcile(store, args.dry_run)
        ctx, sources, root = _scan(env, cfg)
        scan.overlay_archive(root, store.records, cfg.retention_days)
        nodes = _resolve(root, args.targets)
        plan = build_plan(root, {n.id: "archive" for n in nodes}, retention_days=pinned)
        if not plan.of("archive"):
            _print_plan(plan, "archive", "archive")
            _err("nothing here can be archived")
            return 3
        days = pinned or cfg.retention_days
        _out(f"{'Would archive' if args.dry_run else 'Archived'} (nothing is moved; offered for deletion "
             f"after {days} days{'' if pinned else ', from config'}):")
        _print_plan(plan, "archive", "archive")
        results = _executor(env, ctx, store, _guard(env, ctx, sources), args.dry_run).run(plan)
        return 0 if args.dry_run else _report_results(results)


def _archive_list(env: Env, args) -> int:
    cfg = _config(env, args)
    store = _reconciled_store(env, args.dry_run, quiet=args.as_json)
    now = env.now()
    records = store.expired(now, cfg.retention_days) if args.expired else list(store.records)
    if args.as_json:
        _out(json.dumps([_record_json(r, cfg.retention_days, now) for r in records], ensure_ascii=False))
        return 0
    if not records:
        _out("nothing is archived" if not args.expired else "nothing has expired")
        return 0
    for record in sorted(records, key=lambda r: expires_at(r, cfg.retention_days)):
        left = (expires_at(record, cfg.retention_days) - now) / 86400
        when = "expired, will be offered for deletion" if left <= 0 else f"{left:.0f} days left"
        pinned = " (pinned)" if record.get("retention_days") is not None else ""
        _out(f"{human_size(record_bytes(record)):>10}  {record['label']}")
        _out(f"{'':>10}  archived {record['archived_at'][:10]}, {when}{pinned}  [{record['id']}]")
    return 0


def _archive_remove(env: Env, args) -> int:
    with locked(env.state_dir):
        store = _store(env)
        removed = []
        for target in args.targets:
            wanted = os.path.realpath(os.path.expanduser(target)) if target.startswith(("/", "~")) else None
            hits = [r for r in store.records if r["id"] == target]
            if not hits and wanted:
                hits = [r for r in store.records if any(os.path.realpath(p["path"]) == wanted for p in r["parts"])]
            if not hits and len(target) >= 4:
                hits = [r for r in store.records if r["id"].rsplit("/", 1)[-1].startswith(target)]
            if len(hits) > 1:
                raise Fail(f"{target!r} matches {len(hits)} archived items: "
                           + ", ".join(r["id"] for r in hits), 2)
            if not hits:
                raise Fail(f"nothing archived matches {target!r}", 3)
            removed.append(hits[0])
        for record in removed:
            _out(f"unarchived {record['label']}")
            if not args.dry_run:
                store.remove(record["id"])
                from .actions import append_log

                append_log(env.state_dir, {"action": "unarchive", "id": record["id"], "label": record["label"],
                                           "paths": [p["path"] for p in record["parts"]], "result": "ok"})
        if not args.dry_run:
            store.save()
    return 0


def cmd_sweep(env: Env, args) -> int:
    from . import scan
    from .actions import expired_plan

    cfg = _config(env, args)
    with locked(env.state_dir):
        store = _store(env)
        _reconcile(store, args.dry_run)
        now = env.now()
        expired = store.expired(now, cfg.retention_days)
        if not expired:
            _out("nothing archived has expired")
            return 3
        total = sum(record_bytes(r) for r in expired)
        _out(f"{len(expired)} archived item(s) are past their retention period ({human_size(total)}):")
        for record in expired:
            age = human_age(now - expires_at(record, cfg.retention_days))
            _out(f"  {human_size(record_bytes(record)):>10}  {record['label']}  (expired {age} ago)")
        chosen = expired
        if args.dry_run:
            _out("(dry run: nothing deleted)")
            return 0
        if not args.yes:
            if not _is_tty():
                _err("not on a terminal, so nothing was deleted; run `amnesia-sweep sweep` "
                     "on a terminal or pass --yes")
                return 0
            from .prompt import ask, confirm

            answer = ask("Delete them now?", (("d", "delete all"), ("r", "review one by one"),
                                              ("k", f"keep {cfg.retention_days} more days"),
                                              ("s", "skip until next run")), "s")
            if answer == "s":
                _out("skipped; you'll be asked again next run")
                return 0
            if answer == "k":
                for record in expired:
                    store.keep(record["id"], now)
                store.save()
                _out(f"kept for another {cfg.retention_days} days")
                return 0
            if answer == "r":
                chosen = []
                for record in expired:
                    pick = ask(f"  {record['label']} ({human_size(record_bytes(record))})",
                               (("y", "yes, delete"), ("n", "not now"), ("k", "keep longer")), "n")
                    if pick == "y":
                        chosen.append(record)
                    elif pick == "k":
                        store.keep(record["id"], now)
                store.save()
                if not chosen:
                    _out("nothing deleted")
                    return 0
                if not confirm(f"Delete {len(chosen)} item(s) permanently?"):
                    _out("nothing deleted")
                    return 0
        ctx = scan.make_context(env, cfg)
        guard = _guard(env, ctx)
        guard.repos |= {os.path.realpath(p["repo"]) for r in chosen for p in r["parts"] if p.get("repo")}
        results = _executor(env, ctx, store, guard, False).run(expired_plan(chosen))
        return _report_results(results)


def cmd_config(env: Env, args) -> int:
    sub = getattr(args, "config_command", None) or "get"
    if sub == "path":
        _out(config_mod.config_path(env.config_dir))
        return 0
    try:
        cfg = config_mod.load(env.config_dir)
        if sub == "get":
            value = config_mod.get(cfg, getattr(args, "key", None))
            _out(json.dumps(value, indent=2, ensure_ascii=False) if isinstance(value, (dict, list))
                 else json.dumps(value))
            return 0
        value = config_mod.set_value(cfg, args.key, args.value)
        if args.dry_run:
            _out(f"would set {config_mod.normalize_key(args.key)} = {json.dumps(value)}")
            return 0
        path = config_mod.save(cfg, env.config_dir)
    except config_mod.ConfigError as error:
        raise Fail(str(error), 2)
    _out(f"{config_mod.normalize_key(args.key)} = {json.dumps(value)}  ({path})")
    return 0


def cmd_log(env: Env, args) -> int:
    from .actions import read_log

    entries = read_log(env.state_dir, args.n)
    if args.as_json:
        _out(json.dumps(entries, ensure_ascii=False))
        return 0
    if not entries:
        _out("nothing logged yet")
        return 0
    for entry in entries:
        size = human_size(entry["bytes"]) if entry.get("bytes") else ""
        error = f"  ({entry['error']})" if entry.get("error") else ""
        _out(f"{entry.get('ts', '')}  {entry.get('action', ''):<14} {entry.get('result', ''):<7} "
             f"{size:>10}  {entry.get('label', '')}{error}")
    return 0


def cmd_tui(env: Env, args) -> int:
    if not _is_tty():
        raise Fail("the interactive browser needs a terminal; try `amnesia-sweep scan`", 2)
    from .tui import run

    return run(env, _config(env, args), args)


def main(argv: Optional[List[str]] = None) -> int:
    """Run amnesia-sweep and return its process exit code."""
    try:
        args = _parser().parse_args(argv)
        env = Env.from_environ()
        command = args.command
        if command is None:
            command = "tui" if _is_tty() else "scan"
            if command == "scan":
                args = _parser().parse_args(["scan", *(argv if argv is not None else sys.argv[1:])])
                _err("(not a terminal, so printing the overview; run `amnesia-sweep tui` on a terminal)")
        handler = COMMANDS.get(command)
        if handler is None:
            raise Fail(f"unknown command {command}", 2)
        return handler(env, args)
    except _ArgumentExit as error:
        return error.status
    except Fail as error:
        _err(f"amnesia-sweep: {error}")
        return error.status
    except LockHeld as error:
        _err(f"amnesia-sweep: {error}")
        return 1
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
    except BrokenPipeError:
        _quiet_broken_pipe()
        return 0


COMMANDS = {"scan": cmd_scan, "delete": cmd_delete, "archive": cmd_archive, "sweep": cmd_sweep,
            "config": cmd_config, "log": cmd_log, "tui": cmd_tui}
