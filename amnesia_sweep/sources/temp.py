"""Agent leftovers in the system temp folders.

$TMPDIR is mostly the operating system's own data, so only a strict allowlist of agent patterns
is ever actionable there, and only once it is old enough not to be in use. /tmp/claude-<uid> is
handled by the Claude source, and worktrees under /tmp by the worktrees source.
Standard library only.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
from typing import List, Tuple

from ..liveness import pid_alive
from ..model import Node
from .base import ScanContext, Source, group, leaf, ls, multi_leaf, protected_leaf, slug

# (glob in $TMPDIR, label, risk, minimum age in hours)
TMPDIR_PATTERNS: Tuple[Tuple[str, str, str, float], ...] = (
    ("codex-clipboard-*", "Codex clipboard images", "ephemeral", 1),
    ("claude-*", "Claude Code temp", "ephemeral", 24),
    ("mcp-*", "MCP server sockets and temp", "ephemeral", 24),
    ("spankai-*", "SpankAI test homes", "ephemeral", 24),
    ("forge-*", "forge temp", "ephemeral", 24),
    ("opencode*", "opencode temp", "ephemeral", 24),
    ("gemini-*", "Gemini CLI temp", "ephemeral", 24),
)
SHARED = (("node-compile-cache", "Node compile cache (shared by every Node tool)"),)


class Temp(Source):
    name = "temp"
    label = "Temp files"

    def sockets_dir(self, ctx: ScanContext) -> str:
        return os.path.join(ctx.env.tmp_root, "cc-socks")

    def present(self, ctx: ScanContext) -> bool:
        return True

    def roots(self, ctx: ScanContext) -> List[str]:
        configured = [os.path.dirname(os.path.expanduser(p)) for p in ctx.config.tmp_patterns
                      if os.path.isabs(os.path.expanduser(p))]
        return [ctx.env.tmpdir, self.sockets_dir(ctx), *configured]

    def protected(self, ctx: ScanContext) -> List[str]:
        return [os.path.join(ctx.env.tmp_root, "cc-daemon-*"), os.path.join(ctx.env.tmp_root, "codex-daemon-*"),
                os.path.join(ctx.env.tmpdir, "com.apple.*")]

    def scan(self, ctx: ScanContext) -> Node:
        tool = group(self.name, self.label, kind="tool", path=ctx.env.tmpdir)
        entries = ls(ctx.env.tmpdir)
        patterns = list(TMPDIR_PATTERNS) + [(p, f"Configured: {p}", "ephemeral", 24) for p in ctx.config.tmp_patterns
                                            if not os.path.isabs(os.path.expanduser(p))]
        taken = set()
        for pattern, label, risk, min_age in patterns:
            matches = [e for e in entries if fnmatch.fnmatch(e.name, pattern) and e.path not in taken]
            if not matches:
                continue
            category = tool.add(group(f"{self.name}/{slug(pattern)}", label))
            for entry in matches:
                taken.add(entry.path)
                category.add(leaf(ctx, f"{category.id}/{slug(entry.name)}", entry.name, entry.path,
                                  risk=risk, min_age_hours=min_age))
        for pattern in ctx.config.tmp_patterns:
            if not os.path.isabs(os.path.expanduser(pattern)):
                continue
            hits = sorted(glob.glob(os.path.expanduser(pattern)))
            if hits:
                category = tool.add(group(f"{self.name}/{slug(pattern)}", f"Configured: {pattern}"))
                for path in hits:
                    category.add(leaf(ctx, f"{category.id}/{slug(os.path.basename(path))}",
                                      os.path.basename(path), path, risk="ephemeral", min_age_hours=24))
        for name, label in SHARED:
            path = os.path.join(ctx.env.tmpdir, name)
            if os.path.lexists(path):
                tool.add(protected_leaf(ctx, f"{self.name}/{slug(name)}", label, path,
                                        reason="shared with tools that aren't agents", risk="shared"))

        sockets = self.sockets_dir(ctx)
        if os.path.isdir(sockets):
            stale, live = [], 0
            for entry in ls(sockets):
                match = re.match(r"^(\d+)\.sock$", entry.name)
                if match and not pid_alive(int(match.group(1))):
                    stale.append(entry.path)
                else:
                    live += 1
            if stale:
                node = multi_leaf(ctx, f"{self.name}/stale-sockets",
                                  f"Stale Claude Code sockets ({len(stale)}; {live} live kept)",
                                  stale, risk="ephemeral", path=sockets)
                tool.add(node)
        return tool


def sources() -> List[Source]:
    return [Temp()]
