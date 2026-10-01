"""The archive: a list of things you've said you'll probably delete later, and nothing more.

Archiving never moves, renames or modifies anything. It records each path with a fingerprint
(bytes, file count, newest mtime). On every run the archived paths are measured again: a path
that changed is still in use and is quietly unarchived, a path that vanished is dropped, and
records past their retention period are offered for deletion, always with a question first.
Standard library only.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional

from .env import iso, parse_when
from .model import Part
from .sizing import changed, measure

VERSION = 1


class ArchiveError(Exception):
    pass


class LockHeld(ArchiveError):
    pass


def atomic_write(path: str, text: str) -> None:
    """Replace a file's contents so a crash leaves either the old or the new file, never half."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


@contextlib.contextmanager
def locked(state_dir: str) -> Iterator[None]:
    """Hold the state directory's lock, or raise LockHeld if another run has it."""
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "lock"), "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise LockHeld("another amnesia-sweep is running") from error
            raise
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def archived_ts(record: dict) -> float:
    return parse_when(record["archived_at"])


def effective_retention(record: dict, default_days: int) -> int:
    pinned = record.get("retention_days")
    return int(pinned) if isinstance(pinned, int) and not isinstance(pinned, bool) else int(default_days)


def expires_at(record: dict, default_days: int) -> float:
    return archived_ts(record) + effective_retention(record, default_days) * 86400


def record_bytes(record: dict) -> int:
    return sum(int(p.get("bytes", 0)) for p in record.get("parts", []))


@dataclass
class Event:
    kind: str          # gone | unarchived
    record_id: str
    label: str
    path: str


Measure = Callable[[str], Part]


class ArchiveStore:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "archive.json")
        self.data: Dict = {"version": VERSION, "records": []}
        self.loaded = False

    @property
    def records(self) -> List[dict]:
        return self.data["records"]

    def load(self) -> "ArchiveStore":
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self.loaded = True
            return self
        except (OSError, ValueError) as error:
            raise ArchiveError(f"can't read {self.path}: {error}. Fix or move the file; "
                               "amnesia-sweep won't overwrite it.") from error
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise ArchiveError(f"{self.path} isn't an amnesia-sweep archive; fix or move it")
        if int(data.get("version", 1)) > VERSION:
            raise ArchiveError(f"{self.path} was written by a newer amnesia-sweep")
        self.data = data
        self.loaded = True
        return self

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")

    def get(self, record_id: str) -> Optional[dict]:
        for record in self.records:
            if record["id"] == record_id:
                return record
        return None

    def add(self, record_id: str, label: str, source: str, parts: List[Part], now: float,
            retention_days: Optional[int] = None, measure_fn: Measure = measure) -> dict:
        """Record (or re-record) an archive entry, fingerprinting each path freshly."""
        fresh = [measure_fn(p.path) for p in parts]
        stored = []
        for original, current in zip(parts, fresh):
            entry = Part(path=original.path, bytes=current.bytes, files=current.files, newest=current.newest,
                         remover=original.remover, repo=original.repo, branch=original.branch)
            stored.append(entry.to_dict())
        self.remove(record_id)
        record = {"id": record_id, "label": label, "source": source, "archived_at": iso(now),
                  "retention_days": retention_days, "parts": stored}
        self.records.append(record)
        return record

    def remove(self, record_id: str) -> Optional[dict]:
        for i, record in enumerate(self.records):
            if record["id"] == record_id:
                return self.records.pop(i)
        return None

    def drop_paths(self, paths: List[str]) -> None:
        """Forget the given paths everywhere (they were deleted or explicitly unarchived)."""
        doomed = set(paths)
        for record in list(self.records):
            record["parts"] = [p for p in record["parts"] if p["path"] not in doomed]
            if not record["parts"]:
                self.records.remove(record)

    def keep(self, record_id: str, now: float, measure_fn: Measure = measure) -> Optional[dict]:
        """Restart a record's retention period from now."""
        record = self.get(record_id)
        if record is None:
            return None
        parts = [Part.from_dict(p) for p in record["parts"]]
        return self.add(record_id, record["label"], record.get("source", ""), parts, now,
                        record.get("retention_days"), measure_fn)

    def reconcile(self, measure_fn: Measure = measure) -> List[Event]:
        """Drop archived paths that vanished or changed. Returns what happened, in order."""
        events = []
        for record in list(self.records):
            kept = []
            for part in record["parts"]:
                if part.get("remover") == "git-prune":
                    kept.append(part)
                    continue
                current = measure_fn(part["path"])
                if current.missing:
                    events.append(Event("gone", record["id"], record["label"], part["path"]))
                elif changed(part, current):
                    events.append(Event("unarchived", record["id"], record["label"], part["path"]))
                else:
                    kept.append(part)
            record["parts"] = kept
            if not kept:
                self.records.remove(record)
        return events

    def expired(self, now: float, default_days: int) -> List[dict]:
        return [r for r in self.records if now >= expires_at(r, default_days)]

    def path_index(self) -> Dict[str, dict]:
        return {part["path"]: record for record in self.records for part in record["parts"]}
