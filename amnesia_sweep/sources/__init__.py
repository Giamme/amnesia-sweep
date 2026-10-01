"""The registry of harness sources, in the order they appear in the overview.

Adding a harness means adding a Source (or a SpecSource entry in specs.py) and listing it here.
Standard library only.
"""

from __future__ import annotations

from typing import List

from .base import Source


def all_sources() -> List[Source]:
    from . import claude, codex, specs, temp
    from .. import worktrees

    return [*claude.sources(), *codex.sources(), *specs.sources(), *worktrees.sources(), *temp.sources()]
