"""Base-repo ``pre-commit`` guard: stop an aGiTrack worktree-mode agent from committing
straight into the base repo.

The guard is **scoped by an environment marker** so it only ever affects the agent aGiTrack
spawned in worktree mode:

* aGiTrack sets ``AGITRACK_WORKTREE_GUARD=1`` in the agent child's environment — and ONLY
  there, ONLY in worktree mode. Every ``git`` the agent runs inherits it.
* The hook is a no-op unless that variable is present. So the user's own commits, commits from
  an agent run *outside* aGiTrack, and commits from a ``--no-worktree`` agent (none of which
  carry the marker) are never blocked — they commit freely.
* Commits inside a *linked worktree* (the agent's sandbox) are always allowed; only commits in
  the base/main working tree are rejected.

Because the marker gates everything, a hook left behind by a crash is harmless: with no marker
in the environment it simply exits 0 for everyone. A pre-existing project ``pre-commit`` hook is
preserved (moved aside and chained), and restored on removal.

The hook is a POSIX ``sh`` script; Git for Windows runs hooks through its bundled ``sh``, so the
same script works on Windows too.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# Set by aGiTrack on the agent child (worktree mode only); read by the hook.
ENV_GUARD = "AGITRACK_WORKTREE_GUARD"

_MARKER = "# AGITRACK-BASE-COMMIT-GUARD"
_ORIG_SUFFIX = ".agitrack-orig"

_HOOK_SCRIPT = f"""#!/bin/sh
{_MARKER}
# Installed by aGiTrack. Blocks an aGiTrack worktree-mode agent from committing into the base
# repo; a harmless no-op for everyone else (the marker below is set ONLY on that agent's
# process). Remove aGiTrack's worktree sessions and this hook stops doing anything.
if [ -n "${{{ENV_GUARD}}}" ]; then
  case "$(git rev-parse --absolute-git-dir 2>/dev/null)" in
    */worktrees/*)
      : ;;  # inside a linked worktree (the agent's sandbox) -> allowed
    *)
      echo "aGiTrack: this is a worktree session — commit inside your worktree, not the base repo." >&2
      echo "aGiTrack auto-commits and merges your worktree changes for you." >&2
      echo "(If you really mean to commit here, bypass with: git commit --no-verify.)" >&2
      exit 1 ;;
  esac
fi
# Chain to any project pre-commit hook aGiTrack moved aside.
_agitrack_orig="$0{_ORIG_SUFFIX}"
if [ -x "$_agitrack_orig" ]; then
  exec "$_agitrack_orig" "$@"
fi
exit 0
"""


def _make_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass


def is_ours(path: Path) -> bool:
    """Whether *path* is the aGiTrack-installed guard (carries our marker)."""
    try:
        return _MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def install_base_commit_guard(hooks_dir: Path, *, debug: Callable[[str], None] | None = None) -> bool:
    """Install the guard as ``<hooks_dir>/pre-commit`` (idempotent). A pre-existing
    non-aGiTrack hook is moved to ``pre-commit.agitrack-orig`` and chained from ours.
    Returns True on success."""
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-commit"
        orig = hooks_dir / ("pre-commit" + _ORIG_SUFFIX)
        if hook.exists() and not is_ours(hook):
            # Preserve the user's hook (only back up once; don't clobber an existing backup).
            if not orig.exists():
                hook.rename(orig)
                _make_executable(orig)
        hook.write_text(_HOOK_SCRIPT, encoding="utf-8")
        _make_executable(hook)
        return True
    except OSError as error:
        if debug:
            debug(f"install base-commit guard failed: {error!r}")
        return False


def remove_base_commit_guard(hooks_dir: Path, *, debug: Callable[[str], None] | None = None) -> None:
    """Remove the guard and restore any chained original hook. No-op if the current
    ``pre-commit`` isn't ours (we never touch a hook we didn't install)."""
    try:
        hook = hooks_dir / "pre-commit"
        if not hook.exists() or not is_ours(hook):
            return
        orig = hooks_dir / ("pre-commit" + _ORIG_SUFFIX)
        hook.unlink()
        if orig.exists():
            orig.rename(hook)  # restore the project's original hook
            _make_executable(hook)
    except OSError as error:
        if debug:
            debug(f"remove base-commit guard failed: {error!r}")
