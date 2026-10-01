# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow the format `{major}.{minor}.{patch}[.hotfix-{hotfix}]` ([...] parts appear only when non-zero).

## [0.1.0] - 2026-10-01

First release: find what AI coding agents left on your disk, then delete it or archive it for later.

### Highlights

- See how much disk space AI coding agents have left behind: `amnesia-sweep scan` sizes up Claude Code, OpenClaude and Codex sessions, agent-created git worktrees, old CLI versions, caches and temp files, plus Gemini CLI, Grok, Hermes, opencode, Cursor, Continue, LM Studio and other tools when present.
- An interactive browser (run `amnesia-sweep` on a terminal) lets you drill down by tool, project and session, mark items to delete or archive, and review exactly what will happen before anything does.
- Archive things you will probably delete later: nothing is moved or changed, and once the retention period ends (30 days by default, `amnesia-sweep config set retention-days N`) they are offered for deletion on the next run. Anything you touched in the meantime is unarchived automatically.

### Added

- The `scan`, `delete`, `archive`, `sweep`, `config` and `log` commands work without the browser, with `--dry-run`, `--json` output and an audit log of everything removed.
- Live sessions, running versions, locked or busy worktrees, config, credentials and project memory are never offered for removal; worktrees go through `git worktree remove`, and uncommitted changes need an explicit force.
