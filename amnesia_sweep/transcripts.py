"""Bounded reads of agent transcripts: just enough of the head to learn a session's cwd and title.

Transcripts can be hundreds of megabytes with multi-megabyte lines, so nothing here parses a whole
file or JSON-decodes an unbounded line. Standard library only.
"""

from __future__ import annotations

import json
import re
from typing import Dict, Optional

_CWD = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')
_SMALL_LINE = 64 * 1024


def enc(path: str) -> str:
    """Claude Code's project-folder slug: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _unescape(raw: bytes) -> Optional[str]:
    try:
        return json.loads(b'"' + raw + b'"')
    except ValueError:
        return None


def claude_head(path: str, max_lines: int = 200, max_bytes: int = 2 * 1024 * 1024) -> Dict[str, str]:
    """Return {"cwd", "title"} (when found) from the start of a Claude Code transcript."""
    found: Dict[str, str] = {}
    read = 0
    try:
        with open(path, "rb") as handle:
            for number, line in enumerate(handle):
                if number >= max_lines or read >= max_bytes:
                    break
                read += len(line)
                if "title" not in found and len(line) <= _SMALL_LINE and (
                        b'"custom-title"' in line or b'"agent-name"' in line):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        record = {}
                    title = record.get("customTitle") or record.get("agentName")
                    if isinstance(title, str) and title:
                        found["title"] = title
                if "cwd" not in found:
                    match = _CWD.search(line, 0, 1024 * 1024)
                    if match:
                        cwd = _unescape(match.group(1))
                        if cwd:
                            found["cwd"] = cwd
                if "cwd" in found and "title" in found:
                    break
    except OSError:
        pass
    return found


def codex_meta(path: str, max_bytes: int = 1024 * 1024) -> Dict[str, str]:
    """Return {"id", "cwd"} from the session_meta record on line 1 of a Codex rollout."""
    try:
        with open(path, "rb") as handle:
            line = handle.readline(max_bytes)
    except OSError:
        return {}
    found: Dict[str, str] = {}
    match = _CWD.search(line)
    if match:
        cwd = _unescape(match.group(1))
        if cwd:
            found["cwd"] = cwd
    match = re.search(rb'"payload":\{.*?"id":"([0-9a-f-]{8,})"', line)
    if match:
        found["id"] = match.group(1).decode("ascii")
    return found


def read_json(path: str, max_bytes: int = 1024 * 1024) -> dict:
    """Read a small JSON object file; anything unreadable or oversized gives {}."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            return {}
        value = json.loads(data)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
