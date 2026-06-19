"""Confine the agent's filesystem writes to its session worktree.

aGiTrack runs each backend agent with its working directory set to a git worktree,
but nothing stops the agent from writing elsewhere through absolute paths — in
particular into the base repository that contains the worktree, which silently
bypasses aGiTrack's change tracking and worktree isolation.

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

Confinement targets the **base repo** only. It must never get in the way of the
backend coding agent updating *itself* — Claude Code and OpenCode both self-update
in place by rewriting their own install/state directories (e.g. ``claude`` stages a
download under ``~/.claude/downloads`` and swaps the launcher in ``~/.local/bin``;
``opencode`` replaces its binary under ``~/.opencode``). Those trees normally sit in
``$HOME`` outside the repo, but the sandbox carves them out **explicitly** anyway
(see :func:`agent_writable_dirs`) so an update keeps working even when an agent is
installed under the repo or reached through a symlink the deny rule would otherwise
cover — and so the intent ("the agent's own toolchain is never part of what we
confine") is recorded rather than relying on the base-repo deny happening to miss it.

Where no working sandbox is available, :func:`wrap_command` returns the command
unchanged and the caller falls back to warning when the base working tree is
touched.
"""

from __future__ import annotations

import functools
import os
from agitrack.env import getenv_compat
import shutil
import subprocess
import sys

_DISABLE_VALUES = {"0", "off", "false", "no"}


def is_enabled() -> bool:
    """Whether worktree confinement is requested (the ``AGITRACK_SANDBOX`` env var
    overrides everything; default on)."""
    value = (getenv_compat("SANDBOX") or "").strip().lower()
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
# Backend agent self-update carve-out
# ----------------------------------------------------------------------------

# The coding-agent CLIs aGiTrack drives. Their self-update writes land under a small
# set of well-known per-user trees, plus wherever the executable itself resolves
# (covers npm-global / Homebrew / custom prefixes the static list can't predict).
_BACKEND_EXES = ("claude", "opencode")


def _xdg_dir(var: str, *default: str) -> str:
    home = os.path.expanduser("~")
    return os.environ.get(var) or os.path.join(home, *default)


def agent_writable_dirs() -> list[str]:
    """Directories a backend coding agent writes to when it updates itself in place.

    Returned paths are realpath-resolved (so a symlinked ``~/.claude`` matches the
    real path the kernel checks writes against) and de-duplicated. The sandbox keeps
    these writable so a backend self-update is never blocked by worktree confinement.
    Covers Claude Code and OpenCode across native, npm-global, and Homebrew installs:
    the static XDG/HOME roots they use, plus the resolved location of each CLI on PATH.
    """
    home = os.path.expanduser("~")
    data = _xdg_dir("XDG_DATA_HOME", ".local", "share")
    state = _xdg_dir("XDG_STATE_HOME", ".local", "state")
    config = _xdg_dir("XDG_CONFIG_HOME", ".config")
    cache = _xdg_dir("XDG_CACHE_HOME", ".cache")

    candidates = [
        os.path.join(home, ".local", "bin"),  # native launcher symlink (claude)
        os.path.join(home, ".claude"),  # claude config + ~/.claude/downloads staging
        os.path.join(home, ".opencode"),  # opencode install root (bin + node_modules)
    ]
    for tool in _BACKEND_EXES:
        candidates += [os.path.join(root, tool) for root in (data, state, config, cache)]
    # Wherever the CLIs actually resolve — the launcher dir and the install dir the
    # launcher points at (e.g. claude's versioned native install, an npm-global bin).
    for exe in _BACKEND_EXES:
        found = shutil.which(exe)
        if not found:
            continue
        candidates.append(os.path.dirname(found))
        candidates.append(os.path.dirname(os.path.realpath(found)))

    resolved: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        real = os.path.realpath(path)
        if real and real != os.sep and real not in seen:
            seen.add(real)
            resolved.append(real)
    return resolved


def _within(path: str, root: str) -> bool:
    """Whether realpath ``path`` is ``root`` or nested inside it."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives / not comparable
        return False


# ----------------------------------------------------------------------------
# macOS: sandbox-exec profile
# ----------------------------------------------------------------------------


def _quote(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def build_profile(base: str, worktree: str) -> str:
    """A ``sandbox-exec`` profile: allow everything, then deny writes inside the
    base repo *and* the worktrees root (so sibling sessions are off-limits too),
    then re-allow the repo's ``.git``, *this* session's worktree, and the backend
    agent's own install/update dirs. Later rules win, so the worktree allow
    overrides the worktrees-root deny, and the agent-toolchain allows override the
    base deny for any agent dir that happens to live under the repo (keeping a
    backend self-update working)."""
    base = os.path.realpath(base)
    worktree = os.path.realpath(worktree)
    worktrees_root = os.path.dirname(worktree)  # sibling sessions live alongside
    lines = [
        "(version 1)",
        "(allow default)",
        f'(deny file-write* (subpath "{_quote(base)}"))',
        f'(deny file-write* (subpath "{_quote(worktrees_root)}"))',
        f'(allow file-write* (subpath "{_quote(os.path.join(base, ".git"))}"))',
        f'(allow file-write* (subpath "{_quote(worktree)}"))',
    ]
    # Keep the backend coding agent able to update itself: re-allow writes to its
    # install/state/config/download dirs even when they sit under the base repo.
    for path in agent_writable_dirs():
        lines.append(f'(allow file-write* (subpath "{_quote(path)}"))')
    return "\n".join(lines)


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
        "--dev-bind",
        "/",
        "/",  # mirror the whole host, read-write
        "--ro-bind",
        base,
        base,  # ...but the base repo (and its worktrees) read-only
    ]
    if os.path.isdir(git_dir):
        args += ["--bind", git_dir, git_dir]  # re-allow commits into the repo's .git
    args += [
        "--bind",
        worktree,
        worktree,  # re-allow this session's worktree
    ]
    # Re-allow the backend agent's own install/update dirs. Dirs outside the base
    # repo are already read-write via the top ``--dev-bind / /``; only the ones that
    # live *under* the read-only base need re-binding (and only if they exist —
    # bwrap errors on a missing bind source) so a backend self-update isn't blocked.
    for path in agent_writable_dirs():
        if _within(path, base) and os.path.exists(path):
            args += ["--bind", path, path]
    args += [
        "--chdir",
        worktree,
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
