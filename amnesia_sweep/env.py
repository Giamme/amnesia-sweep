"""Every filesystem root amnesia-sweep looks at, plus the clock, in one injectable object.

Nothing else in the package reads os.environ or calls time.time() directly, so tests can point
the whole tool at a fake HOME and a fake /tmp. Standard library only.
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def parse_when(text: str) -> float:
    """Parse epoch seconds or an ISO 8601 timestamp (a trailing Z means UTC)."""
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = _dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.timestamp()


def iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Env:
    home: str
    uid: int
    tmp_root: str                  # /private/tmp on macOS
    tmpdir: str                    # $TMPDIR, e.g. /private/var/folders/xx/yyy/T
    state_dir: str
    config_dir: str
    claude_dir: str
    codex_dir: str
    fixed_now: Optional[float] = None
    environ: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_environ(cls, environ: Optional[Mapping[str, str]] = None) -> "Env":
        environ = dict(os.environ if environ is None else environ)
        home = _real(environ.get("AMNESIA_SWEEP_HOME") or environ.get("HOME") or os.path.expanduser("~"))
        tmp_root = _real(environ.get("AMNESIA_SWEEP_TMP") or "/tmp")
        tmpdir = _real(environ.get("TMPDIR") or tempfile.gettempdir())
        state_base = environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
        config_base = environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        state_dir = _real(environ.get("AMNESIA_SWEEP_STATE_DIR") or os.path.join(state_base, "amnesia-sweep"))
        config_dir = _real(environ.get("AMNESIA_SWEEP_CONFIG_DIR") or os.path.join(config_base, "amnesia-sweep"))
        claude_dir = _real(environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"))
        codex_dir = _real(environ.get("CODEX_HOME") or os.path.join(home, ".codex"))
        now = environ.get("AMNESIA_SWEEP_NOW")
        return cls(home=home, uid=os.getuid(), tmp_root=tmp_root, tmpdir=tmpdir,
                   state_dir=state_dir, config_dir=config_dir, claude_dir=claude_dir,
                   codex_dir=codex_dir, fixed_now=parse_when(now) if now else None,
                   environ=environ)

    def now(self) -> float:
        return self.fixed_now if self.fixed_now is not None else time.time()

    def path(self, *parts: str) -> str:
        """A path under HOME."""
        return os.path.join(self.home, *parts)

    @property
    def caches(self) -> str:
        return self.path("Library", "Caches")

    @property
    def app_support(self) -> str:
        return self.path("Library", "Application Support")

    @property
    def claude_tmp(self) -> str:
        return os.path.join(self.tmp_root, f"claude-{self.uid}")

    def tilde(self, path: str) -> str:
        """Abbreviate HOME as ~ for display."""
        if path == self.home:
            return "~"
        if path.startswith(self.home + os.sep):
            return "~" + path[len(self.home):]
        return path
