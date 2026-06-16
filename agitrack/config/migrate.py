"""One-time, in-place migrations from the pre-rename aGiT layout to aGiTrack.

Two on-disk locations moved when aGiT was renamed to aGiTrack:

* the per-repo state directory ``.agit/`` → ``.agitrack/`` (holds ``state.json``,
  ``config.json``, the lock, and the session worktrees);
* the user-global config directory ``~/.agit/`` → ``~/.agitrack/``.

These helpers move/copy the old layout into place the first time aGiTrack runs so
existing sessions, settings, and worktrees keep working. They are best-effort and
never raise: a migration failure must not stop aGiTrack from starting."""

from __future__ import annotations

import shutil
from pathlib import Path

LEGACY_STATE_DIRNAME = ".agit"
STATE_DIRNAME = ".agitrack"


def migrate_repo_state(repo) -> bool:
    """Move a repo's legacy ``.agit/`` state dir to ``.agitrack/`` and re-link any
    session worktrees that moved with it. ``repo`` is a :class:`GitRepo`. Returns
    True when a migration happened. No-op if the new dir already exists or there is
    no old dir."""
    root = Path(repo.repo)
    legacy = root / LEGACY_STATE_DIRNAME
    current = root / STATE_DIRNAME
    if current.exists() or not legacy.is_dir():
        return False
    try:
        legacy.rename(current)
    except OSError:
        return False
    # Moving the dir orphans each worktree's $GIT_DIR/worktrees/<id>/gitdir
    # back-pointer; repair re-links them. A moved worktree has to be named by its
    # NEW path, so pass every session worktree dir that came across in the move.
    try:
        worktrees = current / "worktrees"
        moved = [str(p) for p in worktrees.iterdir() if p.is_dir()] if worktrees.is_dir() else []
        repo.repair_worktrees(*moved)
    except Exception:
        pass
    return True


def migrate_global_config(new_dir: Path) -> bool:
    """Seed ``~/.agitrack/`` from a legacy ``~/.agit/`` the first time aGiTrack runs.

    Copies (does not move) the legacy config so an older aGiT on the same machine
    keeps working. ``new_dir`` is the resolved aGiTrack config dir. No-op if it
    already exists or there is no legacy dir. Returns True when a copy happened."""
    new_dir = Path(new_dir)
    if new_dir.exists():
        return False
    legacy = new_dir.parent / LEGACY_STATE_DIRNAME
    if not legacy.is_dir():
        return False
    try:
        shutil.copytree(legacy, new_dir)
        return True
    except OSError:
        return False
