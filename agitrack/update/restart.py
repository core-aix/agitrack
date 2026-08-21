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

EVERY daemon swaps the same way: SPAWN-AND-VERIFY. Spawn the replacement, wait for it to
prove it is up (its own handshake), and only exit once it has — a replacement that crashes
on a broken update fails verification, and the old daemon keeps running on the code it
already loaded and retries later. The dashboard and backtrace daemons do this inline in
their serve loops; the background tracker goes through :func:`exec_replacement`
(:func:`agitrack.daemons._daemon_command`, which also handles frozen builds), releasing the
repo lock before the spawn and taking it back if the successor never appears.

Both platforms took a detour to get here, and for the same reason. Windows was a bare
``Popen`` + ``os._exit(0)``: fire-and-forget, unable to tell a successor that took over from
one that died on the way up, so a failed restart left the repo with no tracker at all —
silently, with a success exit code. POSIX was ``os.execv``, which cannot keep the guarantee
at all: exec replaces the process image, so a successor that dies on import leaves nothing
behind to fall back to. Measured on a real pip install with a broken upgrade staged: the
tracker logged "restarting on the new version", exec'd, the successor died on a SyntaxError,
and the repository was left untracked — while the dashboard hub in the same experiment
survived, because it verified. The objection to verifying on POSIX was that the old daemon
held the repo lock and a handoff would deadlock on it; it now releases that lock before
spawning, which removes the deadlock and the silence together.
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


# How long a view daemon (dashboard, hub, backtrace) will hold its own restart back waiting
# for the background trackers to pick the update up first. A tracker needs two consecutive
# one-minute checks plus its handoff, so the ordinary wait is two or three minutes; this cap
# exists for the tracker that never comes back — wedged, or stopped between the two readings —
# where serving the old dashboard forever would be the worse failure.
DEFER_LIMIT_SECONDS = 600.0


def stale_background_trackers() -> list[str]:
    """Repositories whose BACKGROUND TRACKER is still running pre-update code.

    WHY THE VIEW DAEMONS WAIT FOR THIS. Every daemon watches the same fingerprint, so they all
    notice one update at once and race to restart — and the dashboard, which has the least to do
    on the way up, kept winning. The user then got a page that reloaded itself to announce the new
    version and, in the same breath, warned that the session on this repo was still on the old one
    and should be restarted "when convenient" — a warning about a tracker that was already
    restarting itself, and that was gone by the next poll. Two true statements that read as a
    contradiction, and the only actionable one asked for work nobody had to do.

    So the order is fixed rather than raced: trackers first, views last. By the time the page
    reloads, everything it can report on has already moved, and the stale-session banner is left
    to mean what it is for — an interactive session, which aGiTrack deliberately never restarts
    from under the user.

    A tracker's fingerprint is the one it recorded in the repo lock when it took it, and a
    restart re-takes that lock, so this is a file read per tracked repo. Only ``background``
    daemons count: an interactive TUI holds the same lock and is never restarted, so waiting on
    one would keep a dashboard on old code for as long as the conversation lasts.
    """
    try:
        from agitrack import daemons
        from agitrack.update.selfupdate import instance_fingerprint

        current = disk_fingerprint()
        if not current:
            return []
        stale = []
        for info in daemons.list_running():
            if info.kind != "background" or not info.repo:
                continue
            running = instance_fingerprint(Path(info.repo))
            if running and running != current:
                stale.append(info.repo)
        return stale
    except Exception:
        return []  # never let this gate be the reason a daemon cannot restart


def watch_for_update(
    stop: threading.Event,
    on_update: Callable[[str], None],
    *,
    interval: float = CHECK_SECONDS,
    read_version: Callable[[], str | None] = updated_fingerprint,
    self_update: bool = False,
    self_update_interval: float = SELF_UPDATE_SECONDS,
    defer_while: Callable[[], list[str]] | None = None,
    defer_limit: float = DEFER_LIMIT_SECONDS,
    log: Callable[[str], None] = lambda _message: None,
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

    ``defer_while`` ORDERS restarts that would otherwise race. It is polled once the update is
    confirmed and returns whatever is not ready yet (empty when clear); while it returns
    something, the confirmed fingerprint is HELD — not discarded — and re-checked each interval,
    so the restart happens as soon as the wait clears rather than starting the two-reading
    confirmation over. ``defer_limit`` bounds that wait: a daemon must still get onto the new
    code even when whatever it is waiting for never does. See :func:`stale_background_trackers`,
    the one gate that uses it.
    """

    def _loop() -> None:
        candidate: str | None = None
        deferring_since: float | None = None
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
                deferring_since = None
                continue
            if fresh == candidate:
                if defer_while is not None:
                    waiting_for = _deferral(defer_while)
                    if waiting_for:
                        if deferring_since is None:
                            deferring_since = time.monotonic()
                            log(
                                "aGiTrack updated on disk; holding this restart until the background "
                                f"tracker(s) restart first: {', '.join(waiting_for)}"
                            )
                            continue  # keep the confirmed fingerprint and look again next round
                        if time.monotonic() - deferring_since < defer_limit:
                            continue
                        log(
                            "background tracker(s) did not pick the update up in time "
                            f"({', '.join(waiting_for)}); restarting anyway."
                        )
                try:
                    on_update(fresh)
                except Exception:
                    pass
                return
            candidate = fresh
            deferring_since = None

    thread = threading.Thread(target=_loop, daemon=True, name="agitrack-update-watch")
    thread.start()
    return thread


def _deferral(defer_while: Callable[[], list[str]]) -> list[str]:
    """What the gate is still waiting for. A gate that RAISES answers "nothing": the ordering is
    a courtesy to the reader, never a reason a daemon cannot get onto the new code."""
    try:
        return list(defer_while() or [])
    except Exception:
        return []


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


# How long a spawned replacement gets to prove it is up before the old daemon concludes the
# handoff failed and goes back to tracking. Generous: the successor has to import aGiTrack,
# open the repo, install hooks and take the lock, on a machine that may be busy.
VERIFY_SECONDS = 30.0


def exec_replacement(
    command: list[str],
    log: Callable[[str], None] = print,
    *,
    verify: Callable[[int], bool] | None = None,
    verify_seconds: float = VERIFY_SECONDS,
) -> None:
    """Replace this process with ``command`` (called AFTER the daemon's own cleanup).
    On success this never returns. RETURNING means the restart FAILED — every caller
    treats that as "resume on the current code and retry later", so a failed restart
    never strands a dead daemon (see the retry loops in the daemons).

    ``verify(pid)`` is what makes that contract real, on EVERY platform. The old code was
    ``Popen(...)`` followed immediately by ``os._exit(0)``: fire-and-forget. ``Popen`` returns as
    soon as the process is CREATED, which says nothing about whether it went on to track
    anything — so this function could never "return because the restart failed", and a successor
    that died during startup left the repo with no tracker at all, exit code 0, and nothing in
    the log. That is a measured outcome, not a theoretical one: the successor lost the race for
    the repo lock its dying predecessor still held, decided a live tracker was already running,
    and exited.

    So the handoff is SPAWN-AND-VERIFY, the same one the dashboard and backtrace daemons use:
    spawn, wait for the successor to prove it is up, and only then exit. A successor that does
    not come up is terminated and this returns, so the caller keeps running on the old code —
    which is the whole point of restarting only into an install that works.

    **POSIX used to ``os.execv`` here, and that is exactly the guarantee it could not keep.**
    exec replaces the process image: if the new code cannot even be imported — a half-written
    install, a broken release, a wheel whose ``__main__`` does not parse — there is no old
    process left to fall back to. Measured on a real pip install: with a deliberately broken
    upgrade in place, the tracker logged "restarting on the new version", exec'd, the successor
    died on a SyntaxError, and the repository was left with no tracker at all. The dashboard hub
    in the same experiment survived, because it verifies. Preserving the pid is not worth the
    daemon; a caller that needs the successor's pid reads it from the handshake, which is what
    ``verify`` waits for anyway."""
    import subprocess

    try:
        log(f"aGiTrack updated on disk (running {RUNNING_VERSION}); restarting on the new version.")
        if os.name == "nt":
            from agitrack.proc import console_isolation_kwargs

            spawn_kwargs = console_isolation_kwargs()
        else:
            # The successor has to OUTLIVE us: we exit the moment it proves it is up, and a child
            # left in this process's session would go down with the terminal that started us.
            from agitrack.proc import detach_kwargs

            spawn_kwargs = detach_kwargs()
        child = subprocess.Popen(  # noqa: S603 - our own re-launch command
            command,
            stdout=sys.stdout,
            stderr=sys.stderr,
            **spawn_kwargs,
        )
        if verify is None:
            os._exit(0)
        if _replacement_came_up(child, verify, verify_seconds, log):
            os._exit(0)
        _abandon_replacement(child, log)
    except Exception as error:
        log(f"restart after update failed: {error!r}")


def _replacement_came_up(child, verify: Callable[[int], bool], seconds: float, log: Callable[[str], None]) -> bool:
    """Whether the spawned successor proved it is running, within ``seconds``. A successor that
    EXITS is a failure the moment it exits — no point waiting out the timeout for a dead pid."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if verify(child.pid):
                return True
        except Exception:
            pass  # a verify that cannot answer yet is not a failure; keep waiting
        if child.poll() is not None:
            log(
                f"the replacement exited during startup (code {child.returncode}) without taking "
                "over — see the lines above for what it reported."
            )
            return False
        time.sleep(0.2)
    log(f"the replacement did not come up within {seconds:.0f}s.")
    return False


def _abandon_replacement(child, log: Callable[[str], None]) -> None:
    """Stop a successor that was spawned but never took over, so it cannot come up later and
    race the daemon that is about to resume tracking."""
    try:
        if child.poll() is None:
            child.terminate()
    except Exception:
        pass
    log("keeping the current version running; will retry the restart on the next check.")
