"""Declarative sources for agent tools whose data is just "a folder with a few known parts".

Only the listed patterns are ever actionable; everything else under each root lands in "Other
(not touched)". Tools that aren't installed simply don't show up. Standard library only.
"""

from __future__ import annotations

import os
from typing import List

from .base import Rule, SpecSource, Source

VSCODE_EXTENSIONS = ("github.copilot-chat", "saoudrizwan.claude-dev", "continue.continue",
                     "rooveterinaryinc.roo-cline", "kilocode.kilo-code", "anthropic.claude-code",
                     "openai.chatgpt", "google.geminicodeassist")


def _vscode_rules() -> tuple:
    return tuple(Rule(ext, "Extension storage", risk="history", min_age_hours=24) for ext in VSCODE_EXTENSIONS)


SPECS = (
    SpecSource(name="grok", label="Grok CLI", root=".grok", rules=(
        Rule("sessions/*/*", "Sessions", risk="history", group_parent=True, decode="url"),
        Rule("logs", "Logs", risk="ephemeral", min_age_hours=1),
        Rule("downloads", "Downloads (installer cache)", risk="cache"),
    ), keep=("sessions/*.sqlite*",)),
    SpecSource(name="hermes", label="Hermes", root=".hermes", rules=(
        Rule("sessions/*", "Sessions", risk="history", min_age_hours=1),
        Rule("logs", "Logs", risk="ephemeral", min_age_hours=1),
        Rule("cache", "Caches", risk="cache"),
        Rule("audio_cache", "Caches", risk="cache"),
        Rule("image_cache", "Caches", risk="cache"),
        Rule("installs", "Re-downloadable tools", risk="cache"),
        Rule("tools", "Re-downloadable tools", risk="cache"),
    ), keep=(".env", "config.yaml", "SOUL.md", "state.db*", "memories", "skills", "pairing")),
    SpecSource(name="opencode", label="opencode", root=os.path.join(".local", "share", "opencode"), rules=(
        Rule("log", "Logs", risk="ephemeral", min_age_hours=1),
        Rule("repos", "Repository snapshots", risk="cache", min_age_hours=24),
    ), keep=("opencode.db*",)),
    SpecSource(name="forge", label="forge", root=os.path.join(".local", "state", "forge"), rules=(
        Rule("fractal/runs/*", "Runs", risk="history", min_age_hours=24),
        Rule("fractal/installs", "Re-downloadable tools", risk="cache"),
    )),
    SpecSource(name="lmstudio", label="LM Studio", root=".lmstudio", rules=(
        Rule("models/*", "Downloaded models", risk="model", requires="include_models"),
        Rule("conversations", "Conversations", risk="history"),
        Rule("server-logs", "Logs", risk="ephemeral", min_age_hours=1),
    ), keep=("credentials", "settings.json", "mcp.json")),
    SpecSource(name="gemini", label="Gemini CLI", root=".gemini", rules=(
        Rule("tmp/*", "Sessions & checkpoints", risk="history", min_age_hours=1),
    )),
    SpecSource(name="qwen", label="Qwen Code", root=".qwen", rules=(
        Rule("tmp/*", "Sessions & checkpoints", risk="history", min_age_hours=1),
    )),
    SpecSource(name="copilot", label="GitHub Copilot CLI", root=".copilot", rules=(
        Rule("logs", "Logs", risk="ephemeral", min_age_hours=1),
        Rule("history-session-state", "Sessions", risk="history", min_age_hours=1),
    )),
    SpecSource(name="vscode-agents", label="VS Code agent extensions",
               root=os.path.join("Library", "Application Support", "Code", "User", "globalStorage"),
               rules=_vscode_rules()),
    SpecSource(name="cursor", label="Cursor", root=".cursor", rules=(
        Rule("projects/*", "Projects", risk="history", min_age_hours=24),
        Rule("logs", "Logs", risk="ephemeral", min_age_hours=1),
    ), keep=("mcp.json", "rules", "extensions", "argv.json")),
    SpecSource(name="continue", label="Continue", root=".continue", rules=(
        Rule("sessions", "Sessions", risk="history", min_age_hours=24),
        Rule("index", "Index", risk="cache"),
        Rule("logs", "Logs", risk="ephemeral", min_age_hours=1),
        Rule("dev_data", "Usage data", risk="ephemeral"),
    ), keep=("config.*",)),
    SpecSource(name="codeium", label="Codeium / Windsurf", root=".codeium", rules=(
        Rule("windsurf/cascade", "Cascade sessions", risk="history", min_age_hours=24),
        Rule("database", "Index", risk="cache"),
        Rule("windsurf/implicit", "Index", risk="cache"),
    )),
    SpecSource(name="amp", label="Amp", root=os.path.join(".local", "share", "amp"), rules=(
        Rule("threads", "Threads", risk="history", min_age_hours=24),
        Rule("logs", "Logs", risk="ephemeral", min_age_hours=1),
    )),
    SpecSource(name="factory", label="Factory (droid)", root=".factory", rules=(
        Rule("sessions", "Sessions", risk="history", min_age_hours=24),
        Rule("logs", "Logs", risk="ephemeral", min_age_hours=1),
    )),
    SpecSource(name="cline", label="Cline CLI", root=".cline", rules=(
        Rule("data/tasks", "Tasks", risk="history", min_age_hours=24),
        Rule("logs", "Logs", risk="ephemeral", min_age_hours=1),
    )),
)


def sources() -> List[Source]:
    return list(SPECS)
