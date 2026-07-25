"""Daemons restart themselves after aGiTrack is updated on disk.

aGiTrack never installs updates on its own (that rule stands; see the update marker).
But once something else HAS swapped the code underneath — pip/pipx/brew, the MSI, the
interactive self-update, or a ``git pull`` on a source install — a long-running daemon
keeps executing the old modules until it is restarted, which quietly turns "I updated"
into "the fix didn't work". So every daemon (background tracker, dashboard, backtrace)
watches for that moment and replaces itself with the new code.

Detection compares a FINGERPRINT of the install captured at daemon start against fresh
reads from disk, and only ever acts on a COMPLETED update — restarting into a
half-applied one would just crash the daemon on the new code:

- **Source checkout** (a ``pyproject.toml`` beside the package): the fingerprint is the
  git HEAD commit — "updated" means a NEW COMMIT LANDED. While a pull/checkout is in
  flight (``index.lock`` present) the reading is "unsettled" and nothing happens.
- **Wheel install** (pip/pipx/brew/MSI): the fingerprint is the installed distribution
  version. pip tears down the old ``dist-info`` before writing the new one, so missing
  or unreadable metadata likewise reads as "unsettled".

On top of that, the SAME new fingerprint must be seen on two consecutive checks (a
minute apart) before acting, so even a mid-copy state that happens to parse can never
trigger an exec into a half-written tree.

The restart replaces the process with its own re-launch command
(:func:`agitrack.daemons._daemon_command`, which also handles frozen builds): on POSIX
via ``os.execv`` — the pid is preserved, and Python's file descriptors are close-on-exec
so sockets and the repo lock release at the boundary — and on Windows by spawning a
detached replacement and exiting (exec semantics there would not release handles).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable

from agitrack import __version__ as RUNNING_VERSION

# How often a daemon compares the on-disk version with the running one. Reading a
# pyproject.toml / dist-info is milliseconds; once a minute is plenty.
CHECK_SECONDS = 60.0


def _source_root() -> Path | None:
    """The source checkout root when aGiTrack runs from one (pyproject beside the package)."""
    import agitrack

    root = Path(agitrack.__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").exists() else None


def _git_head(root: Path) -> str | None:
    """The checkout's HEAD commit — or None while a git operation is IN PROGRESS
    (``index.lock`` present: a pull/checkout is mid-flight, nothing is settled yet)."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "index.lock", "HEAD"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except Exception:
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return None
    lock, head = lines
    lock_path = Path(lock) if Path(lock).is_absolute() else root / lock
    if lock_path.exists():
        return None  # update in progress — unsettled
    return head.strip() or None


def disk_fingerprint() -> str | None:
    """What the aGiTrack install on disk currently IS, as a comparable string — or None
    when it cannot be determined or an update is visibly in progress. None always means
    "do not act". Source checkout with git → the HEAD commit; source without git (a
    tarball) → the pyproject version; wheel install → the installed metadata version."""
    root = _source_root()
    if root is not None:
        if (root / ".git").exists():
            head = _git_head(root)
            return f"commit:{head}" if head else None
        from agitrack import _source_version

        version = _source_version()
        return f"version:{version}" if version else None
    from agitrack import _installed_version

    version = _installed_version()
    return f"version:{version}" if version else None


# Captured once, at daemon start. None (couldn't fingerprint the install) disables the
# watcher for this process rather than ever guessing.
RUNNING_FINGERPRINT = disk_fingerprint()


def updated_fingerprint() -> str | None:
    """The new on-disk fingerprint when a COMPLETED update differs from the running
    install, else None (same install, unknown, or update still in progress)."""
    if RUNNING_FINGERPRINT is None:
        return None
    try:
        fresh = disk_fingerprint()
    except Exception:
        return None
    if fresh and fresh != RUNNING_FINGERPRINT:
        return fresh
    return None


def watch_for_update(
    stop: threading.Event,
    on_update: Callable[[str], None],
    *,
    interval: float = CHECK_SECONDS,
    read_version: Callable[[], str | None] = updated_fingerprint,
) -> threading.Thread:
    """Start the watcher thread: calls ``on_update(new_fingerprint)`` ONCE, after the
    same new on-disk fingerprint has been seen on two consecutive checks (see module
    docstring). ``stop`` ends the watch (shared with the daemon's own shutdown event)."""

    def _loop() -> None:
        candidate: str | None = None
        while not stop.wait(interval):
            fresh = read_version()
            if fresh is None:
                candidate = None
                continue
            if fresh == candidate:
                try:
                    on_update(fresh)
                except Exception:
                    pass
                return
            candidate = fresh

    thread = threading.Thread(target=_loop, daemon=True, name="agitrack-update-watch")
    thread.start()
    return thread


def restart_command(extra_args: Iterable[str] = ()) -> list[str]:
    """This daemon's own re-launch command, plus ``extra_args`` (e.g. the bound
    ``--dashboard-port`` so the URL survives the restart) unless the flag is already
    part of the original argv."""
    from agitrack.daemons import _daemon_command

    command = _daemon_command()
    extra = list(extra_args)
    if extra and extra[0] in command:
        return command
    return command + extra


def exec_replacement(command: list[str], log: Callable[[str], None] = print) -> None:
    """Replace this process with ``command`` (called AFTER the daemon's own cleanup).
    On success this never returns. RETURNING means the exec FAILED — every caller
    treats that as "resume on the current code and retry later", so a failed restart
    never strands a dead daemon (see the retry loops in the daemons)."""
    try:
        log(f"aGiTrack updated on disk (running {RUNNING_VERSION}); restarting on the new version.")
        if os.name == "nt":
            import subprocess

            from agitrack.proc import console_isolation_kwargs

            subprocess.Popen(  # noqa: S603 - our own re-launch command
                command,
                stdout=sys.stdout,
                stderr=sys.stderr,
                **console_isolation_kwargs(),
            )
            os._exit(0)
        os.execv(command[0], command)
    except Exception as error:
        log(f"restart after update failed: {error!r}")
