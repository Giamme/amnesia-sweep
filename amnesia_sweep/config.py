"""User settings, stored as JSON at ~/.config/amnesia-sweep/config.json.

Precedence is command-line flag, then this file, then the defaults below. Unknown keys in the
file are kept as they are (a newer version may have written them). Standard library only.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

DEFAULT_AGENT_PATTERNS = [
    # Path fragments.
    "path:.claude/worktrees", "path:.forge-worktrees", "path:.codex/worktrees",
    "path:SpankAI/workspaces", "path:/conductor/", "path:.worktrees/",
    # Branch prefixes.
    "branch:worktree-", "branch:claude/", "branch:codex/", "branch:forge/", "branch:spankai/session-",
    "branch:agent-", "branch:agent/", "branch:cursor/", "branch:copilot/",
]


class ConfigError(Exception):
    pass


@dataclass
class Config:
    retention_days: int = 30
    active_grace_minutes: int = 15
    worktree_roots: List[str] = field(default_factory=lambda: ["~/dev"])
    worktree_max_depth: int = 4
    worktree_agent_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_AGENT_PATTERNS))
    include_models: bool = False
    include_user_content: bool = False
    all_worktrees: bool = False
    disabled_sources: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    tmp_patterns: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict, repr=False)

    def validate(self) -> None:
        if not isinstance(self.retention_days, int) or self.retention_days < 1:
            raise ConfigError("retention_days must be a whole number of days, at least 1")
        if not isinstance(self.active_grace_minutes, int) or self.active_grace_minutes < 0:
            raise ConfigError("active_grace_minutes must be a whole number, 0 or more")
        if not isinstance(self.worktree_max_depth, int) or self.worktree_max_depth < 1:
            raise ConfigError("worktree_max_depth must be at least 1")

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra")
        data.update(extra)
        return data


def _keys() -> Dict[str, Any]:
    defaults = Config()
    return {f.name: getattr(defaults, f.name) for f in fields(Config) if f.name != "extra"}


def normalize_key(key: str) -> str:
    return key.strip().replace("-", "_")


def config_path(config_dir: str) -> str:
    return os.path.join(config_dir, "config.json")


def load(config_dir: str) -> Config:
    path = config_path(config_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return Config()
    except (OSError, ValueError) as error:
        raise ConfigError(f"can't read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    config = Config()
    known = _keys()
    for key, value in raw.items():
        if key in known:
            _assign(config, key, value)
        else:
            config.extra[key] = value
    config.validate()
    return config


def _assign(config: Config, key: str, value: Any) -> None:
    default = _keys()[key]
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, list):
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise ConfigError(f"{key} must be {type(default).__name__}, got {json.dumps(value)}")
    setattr(config, key, value)


def save(config: Config, config_dir: str) -> str:
    from .archive import atomic_write  # local import: archive imports nothing from here

    config.validate()
    path = config_path(config_dir)
    atomic_write(path, json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n")
    return path


def get(config: Config, key: Optional[str] = None) -> Any:
    if key is None:
        return config.to_dict()
    key = normalize_key(key)
    if key not in _keys():
        raise ConfigError(f"unknown setting {key!r}; known: {', '.join(sorted(_keys()))}")
    return getattr(config, key)


def set_value(config: Config, key: str, text: str) -> Any:
    """Set a setting from command-line text, parsed as JSON when it parses."""
    key = normalize_key(key)
    if key not in _keys():
        raise ConfigError(f"unknown setting {key!r}; known: {', '.join(sorted(_keys()))}")
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    trial = copy.deepcopy(config)
    _assign(trial, key, value)
    trial.validate()
    setattr(config, key, value)
    return value
