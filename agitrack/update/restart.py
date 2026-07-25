"""Daemons restart themselves after aGiTrack is updated on disk.

aGiTrack never installs updates on its own (that rule stands; see the update marker).
But once something else HAS swapped the code underneath — pip/pipx/brew, the MSI, the
interactive self-update, or a ``git pull`` on a source install — a long-running daemon
keeps executing the old modules until it is restarted, which quietly turns "I updated"
into "the fix didn't work". So every daemon (background tracker, dashboard, backtrace)
watches for that moment and replaces itself with the new code.

Detection compares the version captured at import (:data:`RUNNING_VERSION`) against a
fresh read of the version on disk (``agitrack._resolve_version()``: the source
checkout's ``pyproject.toml`` first, then installed distribution metadata — the same
priority the running stamp uses). The SAME new version must be seen on two consecutive
checks before acting, so an upgrade that is mid-copy can never trigger an exec into a
half-written tree.

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
from typing import Callable, Iterable

from agitrack import __version__ as RUNNING_VERSION

# How often a daemon compares the on-disk version with the running one. Reading a
# pyproject.toml / dist-info is milliseconds; once a minute is plenty.
CHECK_SECONDS = 60.0


def updated_disk_version() -> str | None:
    """The version now on disk when it differs from the running one, else None."""
    try:
        from agitrack import _resolve_version

        fresh = _resolve_version()
    except Exception:
        return None
    if fresh and fresh != "0.0.0" and fresh != RUNNING_VERSION:
        return fresh
    return None


def watch_for_update(
    stop: threading.Event,
    on_update: Callable[[str], None],
    *,
    interval: float = CHECK_SECONDS,
    read_version: Callable[[], str | None] = updated_disk_version,
) -> threading.Thread:
    """Start the watcher thread: calls ``on_update(new_version)`` ONCE, after the same
    new on-disk version has been seen on two consecutive checks (see module docstring).
    ``stop`` ends the watch (shared with the daemon's own shutdown event)."""

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
    Best-effort: on failure the daemon simply stays down, exactly as if it had been
    stopped — never half-alive on old code."""
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
