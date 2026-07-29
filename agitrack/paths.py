"""Path-SHAPE questions, answered the same way on every platform.

aGiTrack reads paths it did not write: a coding agent's transcript records whatever the
machine it ran on used, and a session can be shared from one machine and read on another.
So "is this absolute?", "is this under that?" and "what is the tail of it?" have to be
answered by looking at the STRING, not by asking the local OS about a path that may have
come from a different one.

The POSIX-only versions of these questions (``path.startswith("/")``,
``path.startswith(base + "/")``, ``"/.agitrack/worktrees/" in path``) all silently answer
NO for a Windows path, which is worse than raising: a reconstruction then showed
``diff --git a/C:\\Users\\...\\hello.py`` instead of ``a/hello.py``, and a resumed session
kept editing the worktree it was supposed to be moved out of.

Everything here works on separator-normalised strings and returns forward slashes, which is
what git, the dashboard and the story pages all display.
"""

from __future__ import annotations

import os
import re

# C:\x, C:/x, and \\server\share (UNC). A bare "C:" with no separator is a drive-relative
# path, not an absolute one, and is deliberately not matched.
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2}[^\\/])")


def slash(path: str) -> str:
    """``path`` with one separator: forward. Windows accepts both, and a transcript can carry
    either (a shared session, a tool that normalised, a shell that did not)."""
    return path.replace("\\", "/")


def is_absolute(path: str) -> bool:
    """Whether ``path`` names a location on its own, in POSIX or Windows shape."""
    return path.startswith(("/", "~")) or bool(_WINDOWS_ABSOLUTE.match(path))


def _fold(path: str) -> str:
    """The form two paths are COMPARED in. Case-folded on Windows, where ``C:\\Repo`` and
    ``c:\\repo`` are one directory and a transcript may well disagree with the caller about
    which one it was."""
    normalised = slash(path).rstrip("/")
    return normalised.casefold() if os.name == "nt" else normalised


def under(path: str, base: str) -> bool:
    """Whether ``path`` is ``base`` itself or something inside it."""
    folded_base = _fold(base)
    if not folded_base:
        return False
    folded = _fold(path)
    return folded == folded_base or folded.startswith(folded_base + "/")


def relative_to(path: str, base: str) -> str | None:
    """``path`` expressed under ``base`` (forward slashes), or None when it is not under it.

    An exact match returns "" - the caller decides what naming the base itself means."""
    if not under(path, base):
        return None
    tail = slash(path)[len(slash(base).rstrip("/")) :]
    return tail.lstrip("/")


def contains_segments(path: str, segments: str) -> bool:
    """Whether ``segments`` (written with forward slashes, e.g. ``/.agitrack/worktrees/``)
    appears in ``path``, whichever separator the path was recorded with."""
    haystack = _fold(path) if os.name == "nt" else slash(path)
    needle = segments.casefold() if os.name == "nt" else segments
    return needle in haystack
