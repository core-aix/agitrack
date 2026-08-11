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

How the swap happens differs by daemon. The dashboard and backtrace daemons do a
SPAWN-AND-VERIFY handoff in their own serve loops: spawn the replacement, wait for its
handshake, and only exit once it provably serves — a replacement that crashes on a
broken update fails verification and the old daemon keeps serving and retries. The
background tracker replaces its process with its own re-launch command via
:func:`exec_replacement` (:func:`agitrack.daemons._daemon_command`, which also handles
frozen builds): on POSIX ``os.execv`` — pid preserved, and file descriptors are
close-on-exec so the repo lock releases at the boundary (a spawn-verify handoff would
deadlock on that lock) — and on Windows a detached spawn + exit.
"""

from __future__ import annotations

import os
import sys
import threading
import time
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

    from agitrack.proc import UTF8_TEXT, console_isolation_kwargs

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "index.lock", "HEAD"],
            cwd=root,
            **UTF8_TEXT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
            # This runs on the update watcher's tick — every CHECK_SECONDS, forever, inside a
            # DETACHED daemon that has no console of its own. Windows then gives the child git
            # a console window, which appears and vanishes on the user's desktop once a minute
            # for as long as the tracker runs. Reported as "new terminals appearing over time".
            **console_isolation_kwargs(),
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


# How often a daemon looks for a newer aGiTrack to install. Far slower than the
# fingerprint poll below (that one only reads a file): this one may hit the network.
SELF_UPDATE_SECONDS = 1800.0


def watch_for_update(
    stop: threading.Event,
    on_update: Callable[[str], None],
    *,
    interval: float = CHECK_SECONDS,
    read_version: Callable[[], str | None] = updated_fingerprint,
    self_update: bool = False,
    self_update_interval: float = SELF_UPDATE_SECONDS,
) -> threading.Thread:
    """Start the watcher thread: calls ``on_update(new_fingerprint)`` ONCE, after the
    same new on-disk fingerprint has been seen on two consecutive checks (see module
    docstring). ``stop`` ends the watch (shared with the daemon's own shutdown event).

    The same thread also periodically SELF-UPDATES when ``self_update`` is set. Watching
    alone would only react to someone else installing a new version, so a machine where the
    only aGiTrack running is a dashboard or backtrace daemon would never update at all; the
    daemons therefore opt in. The attempt is lock-guarded, so when a TUI or another daemon
    is already updating this one simply skips the round.

    This flag is about CALLERS, not about the user: both daemons pass ``self_update=True``,
    so self-updating is on for everyone unless they turn off the global ``self_update``
    setting (which :func:`~agitrack.update.selfupdate.attempt_self_update` honours). The
    parameter defaults off only so that a caller wanting the restart watch alone — tests,
    tooling — cannot start installing updates by omission: it once did, and a test's
    watcher fast-forwarded CI's own checkout mid-run, changing pyproject.toml underneath
    the already-imported version and failing the build.
    """

    def _loop() -> None:
        candidate: str | None = None
        last_self_update = 0.0
        while not stop.wait(interval):
            if self_update:
                now = time.monotonic()
                # First pass runs immediately: a daemon started on a stale install should
                # not wait half an hour to catch up.
                if last_self_update == 0.0 or now - last_self_update >= self_update_interval:
                    last_self_update = now
                    try:
                        from agitrack.update.selfupdate import attempt_self_update

                        attempt_self_update()
                    except Exception:
                        pass  # a daemon must keep serving whatever an update attempt does
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
