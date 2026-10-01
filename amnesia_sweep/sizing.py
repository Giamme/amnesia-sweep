"""A du-style walker: real disk usage of a path without ever following a symlink.

Sizes are st_blocks * 512 (what the disk actually holds, so sparse files and APFS clones don't
inflate totals). Hard links are counted once per measured path, other devices are not entered,
and unreadable or vanishing entries are counted as errors instead of aborting. Standard library only.
"""

from __future__ import annotations

import os
import stat
import threading
from typing import Optional

from .model import Part


class Cancelled(Exception):
    pass


class Meter:
    """Shared running totals so a UI can show progress while a source is still scanning."""

    def __init__(self, cancel: Optional[threading.Event] = None):
        self.files = 0
        self.bytes = 0
        self.cancel = cancel

    def tick(self, files: int, nbytes: int) -> None:
        self.files += files
        self.bytes += nbytes
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()


def _usage(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def measure(path: str, meter: Optional[Meter] = None, **part_fields) -> Part:
    """Measure one path and return it as a Part (extra keyword arguments go to the Part)."""
    part = Part(path=path, **part_fields)
    try:
        top = os.lstat(path)
    except FileNotFoundError:
        part.missing = True
        return part
    except OSError:
        part.errors += 1
        return part
    part.bytes = _usage(top)
    part.newest = top.st_mtime
    if not stat.S_ISDIR(top.st_mode):
        part.files = 1
        if meter is not None:
            meter.tick(1, part.bytes)
        return part

    device = top.st_dev
    seen_inodes = set()
    stack = [path]
    pending_files = 0
    pending_bytes = 0
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except FileNotFoundError:
            continue
        except OSError:
            part.errors += 1
            continue
        with entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    part.errors += 1
                    continue
                if st.st_mtime > part.newest:
                    part.newest = st.st_mtime
                if stat.S_ISDIR(st.st_mode):
                    part.bytes += _usage(st)
                    if st.st_dev == device:
                        stack.append(entry.path)
                    continue
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen_inodes:
                        continue
                    seen_inodes.add(key)
                size = _usage(st)
                part.bytes += size
                part.files += 1
                pending_files += 1
                pending_bytes += size
                if pending_files >= 1000 and meter is not None:
                    meter.tick(pending_files, pending_bytes)
                    pending_files = pending_bytes = 0
    if meter is not None:
        meter.tick(pending_files, pending_bytes)
    return part


def changed(recorded: dict, current: Part, slack: float = 2.0) -> bool:
    """True when a fresh measurement no longer matches a recorded fingerprint."""
    if current.missing:
        return True
    return (current.files != int(recorded.get("files", -1))
            or current.bytes != int(recorded.get("bytes", -1))
            or current.newest > float(recorded.get("newest", 0.0)) + slack)
