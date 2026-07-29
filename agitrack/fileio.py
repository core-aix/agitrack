"""One shared atomic-write helper for every state/marker/cache file.

The write lands in a UNIQUELY-named temporary file in the destination directory, then
renames over the target. The earlier pattern used a FIXED ``<file>.tmp`` sidecar, which
broke the moment two aGiTrack processes saved the same file concurrently (an interactive
session plus a dashboard/export/background process on the same repo): both wrote the one
tmp path, the first rename consumed it, and the second crashed with FileNotFoundError
mid-save. A unique name per write makes concurrent saves last-writer-wins, which is the
intended semantics for every caller, while keeping the atomicity guarantee: a reader (or
a crash/SIGKILL/full disk) can never observe a half-written target file.

The rename itself is retried briefly. On POSIX it always succeeds, but Windows refuses one
(``PermissionError``/``ERROR_ACCESS_DENIED``) while ANY other handle to the destination is
open without delete sharing — the other writer a microsecond into its own replace, a reader
that opened the file to load it, or the virus scanner and search indexer that woke up when
the tmp file appeared. Nothing is wrong in any of those cases and they clear in
milliseconds, so a save that is about to succeed must not be turned into a crash in the
caller: that is exactly the "several aGiTrack processes on one repo" setup this module
exists for.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# ~1s of waiting in all: doubling from 10ms, then capped, so the common case (another
# writer's replace, which lasts microseconds) is waited out almost instantly while a slower
# holder (a scanner opening the new file) still gets its moment. Short enough that a save
# which genuinely cannot happen still reports promptly.
_REPLACE_ATTEMPTS = 12
_REPLACE_BACKOFF_SECONDS = 0.01
_REPLACE_BACKOFF_CAP_SECONDS = 0.1


def _replace_when_allowed(tmp: str, path: Path) -> None:
    """``os.replace(tmp, path)``, waiting out a transient refusal (see the module docstring).
    The last attempt raises, so a permission problem that is real is still reported."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(min(_REPLACE_BACKOFF_SECONDS * 2**attempt, _REPLACE_BACKOFF_CAP_SECONDS))


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` (UTF-8); safe under concurrent writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _replace_when_allowed(tmp, path)
    finally:
        # Reached with the tmp still present only when something above raised between
        # the write and the rename; on success os.replace already consumed it.
        try:
            os.unlink(tmp)
        except OSError:
            pass
