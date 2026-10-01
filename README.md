# amnesia-sweep

`amnesia-sweep` shows how much disk space AI coding agents have left behind, and
lets you delete it or archive it for later. That covers transcripts, file
snapshots, temp folders, old CLI versions, git worktrees and caches. Agents write
all of this and never clean it up. On one machine it added up to 4.9 GB under
`~/.codex`, 1.9 GB for Claude Code, and 16 GB of agent git worktrees, most of
them in `/tmp`.

## What it looks at

| Tool | What amnesia-sweep can remove | What it never touches |
|---|---|---|
| Claude Code (`~/.claude`) | sessions: the transcript, subagent output, file-history, session-env, todos, background jobs and the session's `/tmp/claude-<uid>` folder, all removed together; MCP logs; shell snapshots; telemetry; caches; old backups; old versions in `~/.local/share/claude/versions` | settings, `CLAUDE.md`, skills, agents, commands, plugins, the session registry and its keys, each project's `memory/`, `~/.claude.json` |
| OpenClaude (`~/.openclaude`) | same layout as Claude Code | same as Claude Code |
| Codex (`~/.codex`) | session rollouts grouped by project; old releases of the CLI and app-server daemon; caches, temp and shell snapshots | the current release, `auth.json`, `config.toml`, rules, skills, every sqlite database |
| Git worktrees | worktrees agents created: under `.claude/worktrees`, `.forge-worktrees`, SpankAI workspaces or `/tmp`, on `claude/`, `codex/`, `forge/`, `worktree-` branches, or found in Codex's own logs as `git worktree add` targets | the main checkout, locked worktrees, worktrees a process is working in |
| Hermes, Grok, opencode, forge, Gemini CLI, Qwen, Copilot CLI, Cursor, Continue, Codeium/Windsurf, Amp, Factory, Cline, VS Code agent extensions | sessions, logs, caches and re-downloadable tools, where the layout is known | config files, credentials, memories, databases, anything unrecognised |
| LM Studio | conversations and logs; downloaded models only with `--include-models` | settings, credentials |
| Temp folders | a fixed list of agent patterns in `$TMPDIR`; sockets in `/tmp/cc-socks` left by Claude sessions that have ended | everything else in `$TMPDIR` |

Anything a source doesn't recognise is listed under "Other (not touched)" and
can never be selected.

## Running it

    python3 -m amnesia_sweep            # the interactive browser (on a terminal)
    python3 -m amnesia_sweep scan       # the overview as text

Or install a command:

    scripts/install                     # links ~/.local/bin/amnesia-sweep to this checkout
    amnesia-sweep

It needs Python 3.9 or newer and nothing else. It uses only the standard library,
makes no network calls and has no build step.

### The browser

The browser is a size-sorted tree, like `ncdu`: tool › category › project ›
session. Move with the arrow keys. Mark with `d` to delete or `a` to archive. A
mark on a folder covers everything removable inside it, and `u` on an item inside
a marked folder leaves just that item out. `x` shows exactly what will happen
before anything does: what will be deleted, what will be archived, and what will
be left alone and why. Press `?` for every key.

### Commands

    amnesia-sweep scan [--json] [--depth N] [--min-size 10M] [--older-than 30d] [--ids]
    amnesia-sweep delete TARGET... [--dry-run] [--yes] [--force-dirty] [--delete-merged-branch]
    amnesia-sweep archive add TARGET... [--retention-days N]
    amnesia-sweep archive list [--expired] [--json]
    amnesia-sweep archive remove TARGET...
    amnesia-sweep sweep [--yes]
    amnesia-sweep config get [KEY] | config set KEY VALUE | config path
    amnesia-sweep log [-n 50]

A TARGET can be any of three things:

- an item id from `scan --ids`, such as `codex/releases/standalone/0.154.0-aarch64-apple-darwin`;
- a path an item owns;
- the start of a session id, such as `52fb593c`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | something failed |
| 2 | usage error, including an ambiguous target |
| 3 | nothing found, or nothing to do |
| 130 | interrupted |

## Archiving

Archiving doesn't move, rename or change anything. It records the item's paths in
`~/.local/state/amnesia-sweep/archive.json`, along with a fingerprint of each
path: its size, file count and newest modification time. Every run checks the
fingerprints again:

- **Changed:** you're still using it (for example, you resumed that session), so
  it is unarchived quietly and you're told.
- **Gone:** the archive entry is dropped.
- **Older than the retention period** (30 days by default): it is offered for
  deletion. The browser shows a checklist when it starts. `sweep` asks the same
  question on the command line.

Nothing archived is deleted without that question. The only exception is
`sweep --yes`, which is meant for cron.

Change the retention period with `amnesia-sweep config set retention-days 45`.
Archives follow the current setting unless you pinned their own period with
`--retention-days` when you archived them.

## Safety

- **Every removal is checked again just before it happens**, separately from the
  scan. The path must be inside a known agent folder, reached without going
  through a symlinked parent, and must not be a protected path or contain one. It
  is never HOME, a top-level folder, a source root or a repository's main
  checkout.
- **Symlinks are never followed.** For example, `~/.claude/skills` entries often
  point into source repositories. Deleting a symlink removes only the link.
- **Anything in use is shown but can't be selected:**
  - live sessions (checked against the process start time, so a reused PID
    doesn't fool it);
  - running binaries;
  - active background jobs;
  - anything modified in the last 15 minutes.
- **Worktrees** are removed only with `git worktree remove`. One with uncommitted
  changes needs an explicit force, and branches are only deleted with
  `git branch -d`, which refuses unmerged work.
- **The audit log:** every action is appended to
  `~/.local/state/amnesia-sweep/log.jsonl`. `amnesia-sweep log` shows it.
- **Dry runs:** `--dry-run` shows what would happen and writes nothing.

## Settings

`~/.config/amnesia-sweep/config.json`, edited with `amnesia-sweep config set`:

| Key | Default | Meaning |
|---|---|---|
| `retention_days` | 30 | how long archived items wait before deletion is offered |
| `active_grace_minutes` | 15 | items changed more recently than this are treated as in use |
| `worktree_roots` | `["~/dev"]` | where to look for repositories that own agent worktrees (`/tmp`, `$TMPDIR` and known agent folders are always searched) |
| `worktree_max_depth` | 4 | how deep to look under each root |
| `worktree_agent_patterns` | see `config get` | `path:` fragments and `branch:` prefixes that mark a worktree as agent-made |
| `exclude` | `[]` | glob patterns for paths that must never be removable |
| `tmp_patterns` | `[]` | extra `$TMPDIR` globs (or absolute globs) to offer for cleanup |
| `disabled_sources` | `[]` | sources to skip, such as `["lmstudio"]` |
| `include_models`, `include_user_content`, `all_worktrees` | false | same as the flags |

## Develop

    bash scripts/check.sh               # compileall, then the unittest suite

The suite builds fake home folders in temp directories and never reads or writes
the real ones.

## What it does not do

- It doesn't touch config files, credentials, auto-memory, skills, plugins or any
  tool's database. Codex keeps an index of sessions in sqlite, so after deleting
  Codex sessions that index may still list them.
- It doesn't follow symlinks, cross into other filesystems, or delete anything
  it can't trace to a known agent folder or a git worktree.
- It doesn't move archived items anywhere: there is no archive copy, and
  archiving saves no space until the item is deleted.
- It doesn't clean up for you in the background. Claude Code already deletes old
  transcripts on its own after `cleanupPeriodDays` (30 by default). amnesia-sweep
  shows what is left and lets you decide.
- It doesn't delete git branches unless you ask, and even then only merged ones.
  Branches left without a worktree aren't listed, since they take no space.
- It makes no network calls and sends no telemetry.

## License

MIT. See [LICENSE](LICENSE).
