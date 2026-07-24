"""One shared atomic-write helper for every state/marker/cache file.

The write lands in a UNIQUELY-named temporary file in the destination directory, then
renames over the target. The earlier pattern used a FIXED ``<file>.tmp`` sidecar, which
broke the moment two aGiTrack processes saved the same file concurrently (an interactive
session plus a dashboard/export/background process on the same repo): both wrote the one
tmp path, the first rename consumed it, and the second crashed with FileNotFoundError
mid-save. A unique name per write makes concurrent saves last-writer-wins, which is the
intended semantics for every caller, while keeping the atomicity guarantee: a reader (or
a crash/SIGKILL/full disk) can never observe a half-written target file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` (UTF-8); safe under concurrent writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        # Reached with the tmp still present only when something above raised between
        # the write and the rename; on success os.replace already consumed it.
        try:
            os.unlink(tmp)
        except OSError:
            pass
