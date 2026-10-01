"""Claude Code (and forks with the same layout, like OpenClaude).

Sessions are joined across every folder that carries the session id: the transcript and its
subagent folder under projects/, file-history/, session-env/, todos/, jobs/, and the per-session
temp folder in /tmp/claude-<uid>. Deleting a session therefore reclaims all of it, while the
project's memory/ folder, settings, skills and the session registry are never actionable.
Standard library only.
"""

from __future__ import annotations

import glob
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..env import parse_when
from ..model import Node, make_inactionable
from ..transcripts import claude_head, enc as encode, read_json
from .base import (ScanContext, Source, group, is_dir, leaf, ls, multi_leaf, other_bucket,
                   protected_leaf, slug, unclaimed)

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TODO = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-agent-.*\.json$")
_SNAPSHOT_MS = re.compile(r"-(\d{13})-")
_TERMINAL_JOB = {"done", "failed", "error", "cancelled", "canceled", "exited", "killed", "stopped",
                 "completed", "succeeded"}

# Top-level names that hold configuration, credentials or live state.
CONFIG_NAMES = ("settings.json", "settings.local.json", "statusline.sh", "agents", "commands", "skills",
                "sessions", "daemon", "state", "CLAUDE.md", "keybindings.json", ".credentials.json",
                "mcp-needs-auth-cache.json", "daemon-auth-status.json", "daemon-auth-cooldown", "ide",
                "output-styles", "hooks", "skill-workspaces", ".cc-writes")
CACHE_DIRS = (("telemetry", "ephemeral", 0), ("cache", "cache", 0), ("paste-cache", "cache", 24),
              ("statsig", "cache", 0), ("debug", "ephemeral", 1), ("logs", "ephemeral", 1))


def _version_key(name: str) -> Tuple:
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-]", name))


@dataclass
class _Session:
    sid: str
    enc: Optional[str] = None
    artifacts: List[Tuple[str, str, str]] = None  # (artifact name, path, risk)

    def __post_init__(self):
        self.artifacts = self.artifacts or []


class ClaudeFamily(Source):
    def __init__(self, name: str, label: str, root_attr: str = "", home_rel: str = "",
                 tmp: bool = False, mcp_caches: str = "", binaries: str = "", launcher: str = ""):
        self.name = name
        self.label = label
        self._root_attr = root_attr
        self._home_rel = home_rel
        self._tmp = tmp
        self._mcp = mcp_caches
        self._binaries = binaries
        self._launcher = launcher

    # -- locations ------------------------------------------------------------------------------

    def root(self, ctx: ScanContext) -> str:
        return getattr(ctx.env, self._root_attr) if self._root_attr else ctx.env.path(self._home_rel)

    def tmp_root(self, ctx: ScanContext) -> Optional[str]:
        return ctx.env.claude_tmp if self._tmp else None

    def mcp_root(self, ctx: ScanContext) -> Optional[str]:
        return os.path.join(ctx.env.caches, self._mcp) if self._mcp else None

    def binaries_root(self, ctx: ScanContext) -> Optional[str]:
        return ctx.env.path(self._binaries) if self._binaries else None

    def registry(self, ctx: ScanContext) -> str:
        return os.path.join(self.root(ctx), "sessions")

    def present(self, ctx: ScanContext) -> bool:
        return os.path.isdir(self.root(ctx))

    def roots(self, ctx: ScanContext) -> List[str]:
        return [p for p in (self.root(ctx), self.tmp_root(ctx), self.mcp_root(ctx),
                            self.binaries_root(ctx)) if p]

    def protected(self, ctx: ScanContext) -> List[str]:
        root = self.root(ctx)
        patterns = [os.path.join(root, n) for n in CONFIG_NAMES]
        patterns += [os.path.join(root, "settings*.json*"), os.path.join(root, "projects", "*", "memory"),
                     os.path.join(root, "plugins", "*.json"), os.path.join(root, "plugins", "synced"),
                     os.path.join(root, "plugins", "marketplaces"), os.path.join(root, "plugins", "data"),
                     root + ".json"]
        if not ctx.config.include_user_content:
            patterns += [os.path.join(root, "plans"), os.path.join(root, "history.jsonl")]
        return patterns

    # -- scan -----------------------------------------------------------------------------------

    def scan(self, ctx: ScanContext) -> Node:
        root = self.root(ctx)
        tool = group(self.name, self.label, kind="tool", path=root)
        claimed: List[str] = []

        sessions: Dict[str, _Session] = {}
        projects: Dict[str, List[str]] = defaultdict(list)     # enc -> sids
        project_misc: Dict[str, List[str]] = defaultdict(list)  # enc -> unrecognised paths
        memories: Dict[str, str] = {}

        projects_dir = os.path.join(root, "projects")
        claimed.append(projects_dir)
        for project in ls(projects_dir):
            if not is_dir(project):
                project_misc[""].append(project.path)
                continue
            enc = project.name
            projects.setdefault(enc, [])
            for entry in ls(project.path):
                stem = entry.name[:-6] if entry.name.endswith(".jsonl") else entry.name
                if entry.name == "memory":
                    memories[enc] = entry.path
                elif UUID.match(stem):
                    session = sessions.setdefault(stem, _Session(stem, enc))
                    session.enc = session.enc or enc
                    kind = "transcript" if entry.name.endswith(".jsonl") else "subagents & tool output"
                    session.artifacts.append((kind, entry.path, "history"))
                    if stem not in projects[enc]:
                        projects[enc].append(stem)
                else:
                    project_misc[enc].append(entry.path)

        known = set(sessions)
        orphans: Dict[str, _Session] = {}

        def attach(sid: str, kind: str, path: str, risk: str = "history") -> None:
            target = sessions.get(sid) if sid in known else orphans.setdefault(sid, _Session(sid))
            target.artifacts.append((kind, path, risk))

        for folder, kind in (("file-history", "file-history"), ("session-env", "session-env")):
            base = os.path.join(root, folder)
            claimed.append(base)
            loose = []
            for entry in ls(base):
                if UUID.match(entry.name):
                    attach(entry.name, kind, entry.path, "ephemeral")
                else:
                    loose.append(entry.path)
            project_misc[f"@{folder}"].extend(loose)

        todos = os.path.join(root, "todos")
        if os.path.isdir(todos):
            claimed.append(todos)
            for entry in ls(todos):
                match = _TODO.match(entry.name)
                if match:
                    attach(match.group(1), "todos", entry.path, "ephemeral")
                else:
                    project_misc["@todos"].append(entry.path)

        jobs_node = group(f"{self.name}/jobs", "Background jobs")
        jobs_dir = os.path.join(root, "jobs")
        claimed.append(jobs_dir)
        job_cwds: Dict[str, str] = {}
        for entry in ls(jobs_dir):
            state = read_json(os.path.join(entry.path, "state.json"))
            sid = state.get("sessionId") if isinstance(state.get("sessionId"), str) else None
            if not sid:
                matches = [s for s in known if s.startswith(entry.name)]
                sid = matches[0] if len(matches) == 1 else None
            if isinstance(state.get("cwd"), str) and sid in sessions and sessions[sid].enc:
                job_cwds.setdefault(sessions[sid].enc, state["cwd"])
            active = _job_active(state, ctx.env.now())
            if sid in known:
                attach(sid, "background job" + (" (active)" if active else ""), entry.path, "ephemeral")
                if active:
                    sessions[sid].artifacts[-1] = ("background job (active)", entry.path, "!active")
            else:
                label = state.get("name") or entry.name
                node = leaf(ctx, f"{jobs_node.id}/{slug(entry.name)}", f"{entry.name}  {label}",
                            entry.path, risk="ephemeral", kind="artifact",
                            cwd=state.get("cwd") if isinstance(state.get("cwd"), str) else "",
                            state=str(state.get("state") or ""))
                if active:
                    make_inactionable(node, "background job still active", "job-active")
                jobs_node.add(node)

        tmp_root = self.tmp_root(ctx)
        temp_node = group(f"{self.name}/temp", "Temp files", path=tmp_root or "")
        if tmp_root and os.path.isdir(tmp_root):
            for entry in ls(tmp_root):
                cache_break = re.match(r"^cache-break-state-(.+)\.json$", entry.name)
                if cache_break and UUID.match(cache_break.group(1)):
                    attach(cache_break.group(1), "cache-break state", entry.path, "ephemeral")
                elif is_dir(entry) and entry.name.startswith("-"):
                    for sub in ls(entry.path):
                        if UUID.match(sub.name):
                            attach(sub.name, "temp folder", sub.path, "ephemeral")
                        else:
                            temp_node.add(leaf(ctx, f"{temp_node.id}/{slug(entry.name)}/{slug(sub.name)}",
                                               f"{entry.name}/{sub.name}", sub.path, risk="ephemeral",
                                               min_age_hours=24))
                else:
                    temp_node.add(leaf(ctx, f"{temp_node.id}/{slug(entry.name)}", entry.name, entry.path,
                                       risk="ephemeral", min_age_hours=24))

        mcp_root = self.mcp_root(ctx)
        mcp_by_enc: Dict[str, str] = {}
        if mcp_root:
            for entry in ls(mcp_root):
                if is_dir(entry):
                    mcp_by_enc[entry.name] = entry.path

        # -- build the Sessions category -----------------------------------------------------
        live_encs = set()
        for sid, record in ctx.liveness.live_sessions.items():
            if sid in sessions and sessions[sid].enc:
                live_encs.add(sessions[sid].enc)
            cwd = record.get("cwd")
            if isinstance(cwd, str):
                live_encs.add(encode(cwd))

        sessions_node = tool.add(group(f"{self.name}/sessions", "Sessions"))
        for enc in sorted(projects):
            sids = projects[enc]
            cwd, exact = self._project_cwd(ctx, projects_dir, enc, sids, job_cwds)
            label = ctx.env.tilde(cwd) if cwd else enc
            project = group(f"{sessions_node.id}/{enc}", label, kind="project", cwd=cwd or "",
                            enc=enc, path=os.path.join(projects_dir, enc))
            if not exact:
                project.meta["cwd_approx"] = "1"
            elif cwd and not os.path.lexists(cwd):
                project.flags.add("orphan")
                project.meta["note"] = "the project folder no longer exists"
            for sid in sids:
                project.add(self._session_node(ctx, project.id, sessions[sid]))
            if enc in mcp_by_enc:
                node = leaf(ctx, f"{project.id}/mcp-logs", "MCP server logs", mcp_by_enc.pop(enc),
                            risk="ephemeral", kind="artifact")
                if enc in live_encs:
                    make_inactionable(node, "a live session uses this project")
                project.add(node)
            for path in project_misc.get(enc, []):
                project.add(protected_leaf(ctx, f"{project.id}/other/{slug(os.path.basename(path))}",
                                           os.path.basename(path), path, reason="not recognised; left alone"))
            if enc in memories:
                project.add(protected_leaf(ctx, f"{project.id}/memory", "memory (auto-memory)",
                                           memories[enc], reason="project memory is never touched"))
            if project.children:
                sessions_node.add(project)

        if orphans:
            orphan_node = tool.add(group(f"{self.name}/orphans", "Leftovers without a transcript"))
            for sid in sorted(orphans):
                orphan_node.add(self._session_node(ctx, orphan_node.id, orphans[sid], orphan=True))

        if jobs_node.children:
            tool.add(jobs_node)

        # -- caches, logs, temp ----------------------------------------------------------------
        caches = tool.add(group(f"{self.name}/caches", "Caches & logs"))
        for name, risk, min_age in CACHE_DIRS:
            path = os.path.join(root, name)
            if os.path.lexists(path):
                claimed.append(path)
                caches.add(leaf(ctx, f"{caches.id}/{slug(name)}", name, path, risk=risk,
                                min_age_hours=min_age))
        for name in ("daemon.log",):
            path = os.path.join(root, name)
            if os.path.lexists(path):
                claimed.append(path)
                caches.add(leaf(ctx, f"{caches.id}/{slug(name)}", name, path, risk="ephemeral"))
        self._shell_snapshots(ctx, root, caches, claimed)
        self._backups(ctx, root, caches, claimed)
        self._plugins(ctx, root, caches, tool, claimed)
        for enc, path in sorted(mcp_by_enc.items()):
            mcp_group = caches.child("mcp-logs") or caches.add(
                group(f"{caches.id}/mcp-logs", "MCP logs of projects without transcripts"))
            node = leaf(ctx, f"{mcp_group.id}/{slug(enc)}", enc, path, risk="ephemeral")
            if enc in live_encs:
                make_inactionable(node, "a live session uses this project")
            mcp_group.add(node)
        loose = [p for key in ("", "@file-history", "@session-env", "@todos") for p in project_misc.get(key, [])]
        for path in loose:
            caches.add(protected_leaf(ctx, f"{caches.id}/unrecognised/{slug(os.path.basename(path))}",
                                      os.path.relpath(path, root), path, reason="not recognised; left alone"))

        if temp_node.children:
            tool.add(temp_node)

        binaries = self._binaries_node(ctx)
        if binaries is not None:
            tool.add(binaries)

        # -- user content and config -------------------------------------------------------------
        user = tool.add(group(f"{self.name}/user", "Your content (plans, prompt history)"))
        for name in ("plans", "history.jsonl"):
            path = os.path.join(root, name)
            if os.path.lexists(path):
                claimed.append(path)
                node = leaf(ctx, f"{user.id}/{slug(name)}", name, path, risk="user")
                if not ctx.config.include_user_content:
                    make_inactionable(node, "needs --include-user-content")
                user.add(node)

        config = tool.add(group(f"{self.name}/config", "Config & credentials (never touched)"))
        config_paths = []
        for pattern in (*CONFIG_NAMES, "settings*.json*"):
            config_paths += glob.glob(os.path.join(glob.escape(root), pattern))
        for path in sorted(set(config_paths)):
            claimed.append(path)
            config.add(protected_leaf(ctx, f"{config.id}/{slug(os.path.basename(path))}",
                                      os.path.basename(path), path))
        if os.path.lexists(root + ".json"):
            config.add(protected_leaf(ctx, f"{config.id}/{slug(os.path.basename(root))}-json",
                                      ctx.env.tilde(root + ".json"), root + ".json"))

        bucket = other_bucket(ctx, self.name, unclaimed(root, claimed))
        if bucket is not None:
            tool.add(bucket)
        return tool

    # -- helpers --------------------------------------------------------------------------------

    def _project_cwd(self, ctx: ScanContext, projects_dir: str, enc: str, sids: List[str],
                     job_cwds: Dict[str, str]) -> Tuple[Optional[str], bool]:
        transcripts = []
        for sid in sids:
            path = os.path.join(projects_dir, enc, sid + ".jsonl")
            try:
                transcripts.append((os.lstat(path).st_mtime, path))
            except OSError:
                pass
        for _, path in sorted(transcripts, reverse=True)[:3]:
            cwd = claude_head(path).get("cwd")
            if cwd:
                return cwd, True
        if enc in job_cwds:
            return job_cwds[enc], True
        for record in ctx.liveness.live_sessions.values():
            cwd = record.get("cwd")
            if isinstance(cwd, str) and encode(cwd) == enc:
                return cwd, True
        return None, False

    def _session_node(self, ctx: ScanContext, parent_id: str, session: _Session,
                      orphan: bool = False) -> Node:
        sid = session.sid
        title = ""
        for kind, path, _ in session.artifacts:
            if kind == "transcript":
                title = claude_head(path).get("title", "")
            elif kind == "subagents & tool output" and not title:
                title = read_json(os.path.join(path, "custom-title.json")).get("customTitle", "") or ""
        label = f"{sid[:8]}  {title}".rstrip() if title else sid[:8]
        if orphan:
            label += "  (" + ", ".join(sorted({k for k, _, _ in session.artifacts})) + ")"
        node = group(f"{parent_id}/{sid}", label, kind="session", session_id=sid, title=title)
        active_job = False
        used = set()
        for kind, path, risk in session.artifacts:
            if risk == "!active":
                risk, active_job = "ephemeral", True
            short = slug(kind.split(" (")[0])
            artifact_id = short if short not in used else f"{short}--{slug(os.path.basename(path))}"
            used.add(artifact_id)
            node.add(leaf(ctx, f"{node.id}/{artifact_id}", kind, path, risk=risk, kind="artifact"))
        newest = max((p.newest for leaf_ in node.children for p in leaf_.parts), default=0.0)
        if ctx.liveness.session_live(sid):
            make_inactionable(node, "live session", "live")
        elif active_job:
            make_inactionable(node, "background job still active", "job-active")
        elif ctx.recent(newest):
            make_inactionable(node, "active in the last few minutes", "recent")
        return node

    def _shell_snapshots(self, ctx: ScanContext, root: str, caches: Node, claimed: List[str]) -> None:
        base = os.path.join(root, "shell-snapshots")
        if not os.path.isdir(base):
            return
        claimed.append(base)
        starts = [r.get("startedAt") for r in ctx.liveness.live_sessions.values()
                  if isinstance(r.get("startedAt"), (int, float))]
        oldest_live_ms = min(starts) if starts else None
        node = caches.add(group(f"{caches.id}/shell-snapshots", "shell-snapshots"))
        for entry in ls(base):
            item = leaf(ctx, f"{node.id}/{slug(entry.name)}", entry.name, entry.path, risk="ephemeral",
                        min_age_hours=24)
            match = _SNAPSHOT_MS.search(entry.name)
            if oldest_live_ms is not None and (match is None or int(match.group(1)) >= oldest_live_ms - 3600_000):
                make_inactionable(item, "may belong to a live session")
            node.add(item)

    def _backups(self, ctx: ScanContext, root: str, caches: Node, claimed: List[str]) -> None:
        base = os.path.join(root, "backups")
        if not os.path.isdir(base):
            return
        claimed.append(base)
        entries = sorted(ls(base), key=lambda e: _mtime(e.path))
        if len(entries) > 1:
            caches.add(multi_leaf(ctx, f"{caches.id}/backups", f"backups (all but the newest, {len(entries) - 1})",
                                  [e.path for e in entries[:-1]], risk="cache"))
        if entries:
            caches.add(protected_leaf(ctx, f"{caches.id}/backups-newest", "backups (newest, kept)",
                                      entries[-1].path, reason="the newest backup is kept"))

    def _plugins(self, ctx: ScanContext, root: str, caches: Node, tool: Node, claimed: List[str]) -> None:
        base = os.path.join(root, "plugins")
        if not os.path.isdir(base):
            return
        claimed.append(base)
        config = group(f"{self.name}/plugins", "Plugins (installed; never touched)")
        for entry in ls(base):
            if entry.name == "cache":
                caches.add(leaf(ctx, f"{caches.id}/plugins-cache", "plugins/cache", entry.path, risk="cache"))
            else:
                config.add(protected_leaf(ctx, f"{config.id}/{slug(entry.name)}", entry.name, entry.path))
        if config.children:
            tool.add(config)

    def _binaries_node(self, ctx: ScanContext) -> Optional[Node]:
        base = self.binaries_root(ctx)
        if not base or not os.path.isdir(base):
            return None
        node = group(f"{self.name}/binaries", "Installed versions", path=base)
        entries = [e for e in ls(base) if re.match(r"^\d+\.\d+", e.name)]
        if not entries:
            return None
        newest = max(entries, key=lambda e: _version_key(e.name)).name
        current = None
        if self._launcher:
            launcher = ctx.env.path(self._launcher)
            if os.path.islink(launcher):
                current = os.path.realpath(launcher)
        live = ctx.liveness.live_versions()
        running = ctx.liveness.files_open([e.path for e in entries])
        for entry in entries:
            item = leaf(ctx, f"{node.id}/{entry.name}", entry.name, entry.path, risk="binary", kind="version")
            if current and os.path.realpath(entry.path) == current:
                make_inactionable(item, "the version `claude` runs", "current")
            elif entry.name == newest:
                make_inactionable(item, "newest version", "current")
            elif running is None:
                make_inactionable(item, "couldn't check whether it's running")
            elif (entry.name in live or os.path.realpath(entry.path) in running
                  or ctx.liveness.arg_mentions(entry.path)):
                make_inactionable(item, "a running process uses it", "in-use")
            node.add(item)
        return node


def _mtime(path: str) -> float:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return 0.0


def _job_active(state: dict, now: float) -> bool:
    status = str(state.get("state") or "").lower()
    if status in _TERMINAL_JOB:
        return False
    updated = state.get("updatedAt")
    if isinstance(updated, str):
        try:
            return now - parse_when(updated) < 7 * 86400
        except ValueError:
            return True
    return True


def sources() -> List[Source]:
    return [
        ClaudeFamily("claude", "Claude Code", root_attr="claude_dir", tmp=True,
                     mcp_caches="claude-cli-nodejs", binaries=os.path.join(".local", "share", "claude", "versions"),
                     launcher=os.path.join(".local", "bin", "claude")),
        ClaudeFamily("openclaude", "OpenClaude", home_rel=".openclaude"),
    ]
