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

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

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


def read_json_object(path: Path) -> dict[str, Any]:
    """The JSON object at *path*, or ``{}`` when it is missing, unreadable or not an object."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_json_for_save(path: Path, current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """The document to write for a whole-file JSON store, merged against the file on disk.

    Atomicity is NOT isolation. ``atomic_write_text`` guarantees no reader sees a torn file,
    but a store that serializes its entire in-memory dict still performs a LOST UPDATE: two
    live objects over one path — a second instance in the same process, or a second aGiTrack
    process (background tracker, dashboard daemon, ``--json`` run) on the same repo — each
    hold a private copy taken when they loaded, and whichever saves last silently discards
    every key the other wrote. That is not theoretical: a session name written through a
    second ``AgitrackState`` vanished on the session state's next property setter, leaving
    the conversation unnamed and the name unrecoverable.

    So instead of writing ``current`` blind, re-read the file and apply only what THIS
    instance actually changed since it loaded (*baseline*):

    * a key this instance changed  -> ours wins (a genuine update),
    * a key this instance deleted  -> deleted here too (deletions must propagate),
    * a key this instance never touched -> whatever is on disk wins, so a concurrent
      writer's key survives even when our copy is hours stale,
    * a key we hold that the file has never had -> contributed (first save / new defaults).

    Merging is per top-level key. Two writers updating the SAME key still resolve
    last-writer-wins — that is unavoidable without locking, and it is the narrow case;
    the damage in practice came from unrelated keys being dropped wholesale.
    """
    merged = read_json_object(path)
    for key, value in current.items():
        if key not in baseline or baseline[key] != value or key not in merged:
            merged[key] = value
    for key in baseline:
        if key not in current:
            merged.pop(key, None)
    return merged


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` (UTF-8); safe under concurrent writers."""
    # Writing into `.agitrack/` is what CREATES it, so the self-ignore has to be established
    # here rather than left to whichever caller happens to be first (see ensure_state_dir).
    ensure_state_dir(path.parent)
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


def safe_is_dir(path: Path) -> bool:
    """``path.is_dir()`` that answers False instead of raising on a path the OS will not even
    look at.

    ``Path.is_dir()`` swallows "not found" but NOT ``ENAMETOOLONG``. The coding-agent backends key
    their transcripts by flattening the repo's cwd into ONE filename, so a repo nested ~225
    characters deep produces a component past Linux's 255-byte limit and the probe raised
    ``OSError: [Errno 36] File name too long`` — a raw traceback, exit 1, TUI never starts, from a
    call whose only job was to ask whether a directory exists. A path we cannot stat holds no
    transcripts by definition, which is exactly what False means here."""
    try:
        return path.is_dir()
    except OSError:
        return False


# aGiTrack's per-repo state directory. Everything it writes about a repository lives here.
STATE_DIRNAME = ".agitrack"

_SELF_IGNORE = (
    "# aGiTrack's own working state for this repository. Never part of your project,\n"
    "# and never something you should be asked to commit. Written by aGiTrack itself.\n"
    "*\n"
)


def ensure_state_dir(directory: Path) -> Path:
    """Create ``directory`` and, if it is (or is inside) a repo's ``.agitrack/``, make that
    directory ignore itself. Returns ``directory``.

    ``.git/info/exclude`` was the only thing keeping ``.agitrack/`` out of the user's tree, and
    it is written by a DIFFERENT code path — so every run that created a lock, a config, a
    dashboard log or an export before tracking started, and then exited (no TTY, `-d html`,
    `--remove-hooks`, `--recover`, an abort), left the user staring at ``?? .agitrack/`` in a
    repository aGiTrack had just told them it had never touched. Even the happy path had a race
    where the files landed before the exclude line did.

    A ``.gitignore`` holding ``*`` inside the directory needs no second code path and no ordering:
    the moment the directory exists it is ignored, including the ``.gitignore`` itself. The
    exclude entry is still written — it is what keeps a repo whose ``.agitrack/`` predates this
    clean — but nothing depends on it landing first."""
    directory.mkdir(parents=True, exist_ok=True)
    state_root = _state_root(directory)
    if state_root is None:
        return directory
    marker = state_root / ".gitignore"
    try:
        if not marker.exists():
            marker.write_text(_SELF_IGNORE, encoding="utf-8")
    except OSError:
        pass  # best-effort: a read-only or racing filesystem must never break the caller
    return directory


def _state_root(directory: Path) -> Path | None:
    """The ``.agitrack/`` ancestor of ``directory`` (or itself), or None if there is none."""
    for candidate in (directory, *directory.parents):
        if candidate.name == STATE_DIRNAME:
            return candidate
    return None
