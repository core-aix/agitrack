"""Confine the agent's filesystem writes to its session worktree.

aGiT runs each backend agent with its working directory set to a git worktree,
but nothing stops the agent from writing elsewhere through absolute paths — in
particular into the base repository that contains the worktree, which silently
bypasses aGiT's change tracking and worktree isolation.

Two enforcement mechanisms are supported:

* **macOS** — wrap the backend in ``sandbox-exec`` with a profile that allows
  everything except writing inside the base repo's working tree, re-allowing only
  the repo's ``.git`` (so the worktree can still commit) and the session worktree
  itself.
* **Linux** — wrap the backend in ``bubblewrap`` (``bwrap``): mirror the whole
  host filesystem read-write, re-bind the base repo read-only, then re-bind the
  repo's ``.git`` and *this* session's worktree read-write on top (later binds
  win). Bubblewrap needs unprivileged user namespaces; on hosts where those are
  blocked (e.g. Ubuntu's ``kernel.apparmor_restrict_unprivileged_userns``) the
  runtime probe in :func:`_bwrap_works` fails and confinement reports itself
  unavailable rather than breaking the agent.

In both cases the agent can still *read* base files, so it keeps full context.

Where no working sandbox is available, :func:`wrap_command` returns the command
unchanged and the caller falls back to warning when the base working tree is
touched.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys

_DISABLE_VALUES = {"0", "off", "false", "no"}


def is_enabled() -> bool:
    """Whether worktree confinement is requested (the ``AGIT_SANDBOX`` env var
    overrides everything; default on)."""
    value = os.environ.get("AGIT_SANDBOX", "").strip().lower()
    if value in _DISABLE_VALUES:
        return False
    return True


# ----------------------------------------------------------------------------
# Mechanism detection
# ----------------------------------------------------------------------------


def _have_sandbox_exec() -> bool:
    """macOS ``sandbox-exec`` is present."""
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


@functools.cache
def _bwrap_works() -> bool:
    """Whether ``bwrap`` is installed *and* can actually create a sandbox here.

    Presence of the binary is not enough: bubblewrap needs unprivileged user
    namespaces, which several hardened kernels (notably Ubuntu 24.04 with
    ``kernel.apparmor_restrict_unprivileged_userns=1``) deny. We run a cheap
    no-op sandbox once and cache the result, so a blocked host degrades to the
    warn-on-base-edit fallback instead of failing every backend launch.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False
    true_bin = shutil.which("true") or "/bin/true"
    try:
        result = subprocess.run(
            [bwrap, "--dev-bind", "/", "/", "--", true_bin],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _have_bwrap() -> bool:
    """Linux ``bubblewrap`` is present and usable."""
    return sys.platform.startswith("linux") and _bwrap_works()


def is_available() -> bool:
    """Whether this platform can enforce confinement right now."""
    return _have_sandbox_exec() or _have_bwrap()


# ----------------------------------------------------------------------------
# macOS: sandbox-exec profile
# ----------------------------------------------------------------------------


def _quote(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def build_profile(base: str, worktree: str) -> str:
    """A ``sandbox-exec`` profile: allow everything, then deny writes inside the
    base repo *and* the worktrees root (so sibling sessions are off-limits too),
    then re-allow the repo's ``.git`` and *this* session's worktree. Later rules
    win, so the worktree allow overrides the worktrees-root deny."""
    base = os.path.realpath(base)
    worktree = os.path.realpath(worktree)
    worktrees_root = os.path.dirname(worktree)  # sibling sessions live alongside
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            f'(deny file-write* (subpath "{_quote(base)}"))',
            f'(deny file-write* (subpath "{_quote(worktrees_root)}"))',
            f'(allow file-write* (subpath "{_quote(os.path.join(base, ".git"))}"))',
            f'(allow file-write* (subpath "{_quote(worktree)}"))',
        ]
    )


# ----------------------------------------------------------------------------
# Linux: bubblewrap command
# ----------------------------------------------------------------------------


def build_bwrap_command(base: str, worktree: str) -> list[str]:
    """A ``bwrap`` prefix that mirrors the host filesystem read-write, re-binds
    the base repo read-only (covering sibling worktrees), then re-binds the
    repo's ``.git`` and *this* session's worktree read-write on top. Bind order
    matters: later binds layer over earlier ones, so the worktree/``.git`` binds
    must follow the base read-only bind to win."""
    base = os.path.realpath(base)
    worktree = os.path.realpath(worktree)
    git_dir = os.path.join(base, ".git")
    args = [
        "bwrap",
        "--dev-bind", "/", "/",  # mirror the whole host, read-write
        "--ro-bind", base, base,  # ...but the base repo (and its worktrees) read-only
    ]
    if os.path.isdir(git_dir):
        args += ["--bind", git_dir, git_dir]  # re-allow commits into the repo's .git
    args += [
        "--bind", worktree, worktree,  # re-allow this session's worktree
        "--chdir", worktree,
        "--die-with-parent",
        "--",
    ]
    return args


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def wrap_command(command: list[str], *, base: str, worktree: str) -> list[str]:
    """Wrap ``command`` so its writes are confined to ``worktree`` (plus the
    repo's ``.git``) within ``base``. Returns the command unchanged when
    confinement is disabled, unavailable, or unnecessary (worktree == base)."""
    if not command or not is_enabled():
        return command
    if os.path.realpath(worktree) == os.path.realpath(base):
        return command  # legacy in-place session: nothing to isolate
    if _have_sandbox_exec():
        return ["sandbox-exec", "-p", build_profile(base, worktree), *command]
    if _have_bwrap():
        return [*build_bwrap_command(base, worktree), *command]
    return command
