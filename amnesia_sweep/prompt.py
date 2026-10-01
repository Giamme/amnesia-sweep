"""Line-mode questions for the non-TUI commands, with injectable input and output for tests.

Standard library only.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence, TextIO, Tuple


def ask(question: str, choices: Sequence[Tuple[str, str]], default: str,
        stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> str:
    """Ask until one of the choice keys is typed; an empty answer or EOF gives the default."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stderr
    keys = [key for key, _ in choices]
    menu = " / ".join(f"[{key}]{text}" if text.lower().startswith(key) else f"[{key}] {text}"
                      for key, text in choices)
    while True:
        stdout.write(f"{question} {menu} (default {default}): ")
        stdout.flush()
        line = stdin.readline()
        if not line:
            stdout.write("\n")
            return default
        answer = line.strip().lower()
        if not answer:
            return default
        for key, text in choices:
            if answer in (key, text.lower()):
                return key
        stdout.write(f"please answer {', '.join(keys)}\n")


def confirm(question: str, default: bool = False, stdin: Optional[TextIO] = None,
            stdout: Optional[TextIO] = None) -> bool:
    answer = ask(question, (("y", "yes"), ("n", "no")), "y" if default else "n", stdin, stdout)
    return answer == "y"
