"""OpenAI Codex CLI (~/.codex, or $CODEX_HOME).

Session rollouts are grouped by the cwd recorded on their first line. Old releases of the
standalone CLI and the app-server daemon are actionable except the one `current` points at and
any a running process uses. Codex's sqlite databases, auth, config, rules and skills are never
touched. Standard library only.
"""

from __future__ import annotations

import glob
import os
import re
from collections import defaultdict
from typing import Dict, List

from ..model import Node, make_inactionable
from ..transcripts import codex_meta
from .base import ScanContext, Source, group, is_dir, leaf, ls, other_bucket, protected_leaf, slug, unclaimed

CONFIG_NAMES = ("auth.json", "config.toml", "hooks.json", "rules", "skills", "installation_id",
                "app-server-daemon", "app-server-control", "AGENTS.md", "alert-sound.sh", "version.json",
                ".sandbox_migration", "thread-writer-locks", "tui-thread-reference-capabilities",
                "history.jsonl", "session_index.jsonl")
CACHES = ((".tmp", "ephemeral", 1), ("tmp", "ephemeral", 1), ("cache", "cache", 0),
          ("models_cache.json", "cache", 0), ("log", "ephemeral", 1))
SQLITE_WARNING = ("Codex's thread index (state_5.sqlite, thread_history_1.sqlite) may still list "
                  "deleted sessions; the sqlite files themselves are never touched")


class Codex(Source):
    name = "codex"
    label = "Codex"

    def root(self, ctx: ScanContext) -> str:
        return ctx.env.codex_dir

    def roots(self, ctx: ScanContext) -> List[str]:
        return [self.root(ctx)]

    def protected(self, ctx: ScanContext) -> List[str]:
        root = self.root(ctx)
        return ([os.path.join(root, n) for n in CONFIG_NAMES]
                + [os.path.join(root, "*.sqlite*"), os.path.join(root, "packages", "*", "current"),
                   os.path.join(root, "packages", "*", "*.lock"), os.path.join(root, "plugins", "*.json")])

    def scan(self, ctx: ScanContext) -> Node:
        root = self.root(ctx)
        tool = group(self.name, self.label, kind="tool", path=root)
        claimed: List[str] = [os.path.join(root, "worktrees")]  # owned by the worktrees source
        open_files = ctx.liveness.open_files("codex")

        def held_open(path: str, newest: float) -> bool:
            if open_files is None:  # lsof failed: anything touched today might be open
                return ctx.env.now() - newest < 86400
            return os.path.realpath(path) in open_files

        # Sessions, grouped by cwd.
        sessions = tool.add(group(f"{self.name}/sessions", "Sessions", warning=SQLITE_WARNING))
        by_cwd: Dict[str, List[str]] = defaultdict(list)
        for folder in ("sessions", "archived_sessions"):
            base = os.path.join(root, folder)
            if not os.path.isdir(base):
                continue
            claimed.append(base)
            for path in glob.glob(os.path.join(glob.escape(base), "**", "*.jsonl"), recursive=True):
                cwd = codex_meta(path).get("cwd") or ""
                by_cwd[cwd].append(path)
        for cwd in sorted(by_cwd):
            label = ctx.env.tilde(cwd) if cwd else "(unknown folder)"
            project = sessions.add(group(f"{sessions.id}/{slug(cwd) or 'unknown'}", label, kind="project", cwd=cwd))
            if cwd and not os.path.lexists(cwd):
                project.flags.add("orphan")
            for path in sorted(by_cwd[cwd]):
                name = os.path.basename(path)
                match = re.match(r"^rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d)-\d\d-(.+)\.jsonl$", name)
                label = f"{match.group(1).replace('T', ' ')}  {match.group(2)[-12:]}" if match else name
                node = leaf(ctx, f"{project.id}/{slug(name[:-6])}", label, path, risk="history", kind="session",
                            archived="1" if "/archived_sessions/" in path else "")
                if held_open(path, node.parts[0].newest):
                    make_inactionable(node, "open in a running Codex", "live")
                project.add(node)

        # Releases.
        releases = tool.add(group(f"{self.name}/releases", "Installed versions"))
        packages = os.path.join(root, "packages")
        if os.path.isdir(packages):
            claimed.append(packages)
            for package in ls(packages):
                rel_dir = os.path.join(package.path, "releases")
                if not os.path.isdir(rel_dir):
                    continue
                current_link = os.path.join(package.path, "current")
                current = os.path.realpath(current_link) if os.path.islink(current_link) else None
                pkg = releases.add(group(f"{releases.id}/{slug(package.name)}", package.name))
                for entry in ls(rel_dir):
                    item = leaf(ctx, f"{pkg.id}/{slug(entry.name)}", entry.name, entry.path,
                                risk="binary", kind="version")
                    if current is None:
                        make_inactionable(item, "can't tell which version is current")
                    elif os.path.realpath(entry.path) == current:
                        make_inactionable(item, "current version", "current")
                    elif ctx.liveness.arg_mentions(entry.path) or _holds_inside(open_files, entry.path):
                        make_inactionable(item, "a running process uses it", "in-use")
                    pkg.add(item)

        # Caches & temp.
        caches = tool.add(group(f"{self.name}/caches", "Caches & temp"))
        for name, risk, min_age in CACHES:
            path = os.path.join(root, name)
            if os.path.lexists(path):
                claimed.append(path)
                caches.add(leaf(ctx, f"{caches.id}/{slug(name)}", name, path, risk=risk, min_age_hours=min_age))
        snapshots = os.path.join(root, "shell_snapshots")
        if os.path.isdir(snapshots):
            claimed.append(snapshots)
            snap = caches.add(group(f"{caches.id}/shell-snapshots", "shell_snapshots"))
            for entry in ls(snapshots):
                item = leaf(ctx, f"{snap.id}/{slug(entry.name)}", entry.name, entry.path, risk="ephemeral",
                            min_age_hours=24)
                if held_open(entry.path, item.parts[0].newest):
                    make_inactionable(item, "open in a running Codex", "live")
                snap.add(item)
        plugins = os.path.join(root, "plugins")
        if os.path.isdir(plugins):
            claimed.append(plugins)
            kept = group(f"{self.name}/plugins", "Plugins (installed; never touched)")
            for entry in ls(plugins):
                if entry.name == "cache" and is_dir(entry):
                    caches.add(leaf(ctx, f"{caches.id}/plugins-cache", "plugins/cache", entry.path, risk="cache"))
                else:
                    kept.add(protected_leaf(ctx, f"{kept.id}/{slug(entry.name)}", entry.name, entry.path))
            if kept.children:
                tool.add(kept)

        # Databases and config: shown, never touched.
        databases = tool.add(group(f"{self.name}/databases", "Databases (never touched)"))
        for path in sorted(glob.glob(os.path.join(glob.escape(root), "*.sqlite*"))):
            claimed.append(path)
            databases.add(protected_leaf(ctx, f"{databases.id}/{slug(os.path.basename(path))}",
                                         os.path.basename(path), path,
                                         reason="Codex's own database; clean it up from Codex"))
        config = tool.add(group(f"{self.name}/config", "Config & credentials (never touched)"))
        for name in CONFIG_NAMES:
            path = os.path.join(root, name)
            if os.path.lexists(path):
                claimed.append(path)
                config.add(protected_leaf(ctx, f"{config.id}/{slug(name)}", name, path))

        bucket = other_bucket(ctx, self.name, unclaimed(root, claimed))
        if bucket is not None:
            tool.add(bucket)
        return tool


def _holds_inside(open_files, folder: str) -> bool:
    if open_files is None:
        return False
    prefix = os.path.realpath(folder) + os.sep
    return any(path.startswith(prefix) for path in open_files)


def sources() -> List[Source]:
    return [Codex()]
