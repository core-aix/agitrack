"""``agitrack stop`` — stop aGiTrack for one repository, whatever mode it is running in.

aGiTrack can be holding a repository in several different ways at once: a background tracker
(``-b``), an interactive TUI session, and a dashboard serving that repo. Each had its own stop
command (``-b stop``, ``-d stop``, ``--backtrace stop``, Ctrl-G → quit), which is three things to
remember for one intention — and the user asking usually does not know which of them is running,
only that they want aGiTrack off this repository. So one verb covers all of them.

What ``stop`` deliberately does NOT do:

* reach other repositories. ``--daemons stop`` is the machine-wide sweep; this one is scoped, so
  stopping tracking on the project you are leaving never takes down the dashboard for the project
  you are still working in.
* revoke standing preferences. The background tracker's teardown disarms the auto-start hooks (a
  stop that the next commit undoes is not a stop), but the ``autotrack_hook`` PREFERENCE itself is
  left alone: the next ``agitrack -b`` re-arms it. Only ``--remove-hooks`` opts out for good.
"""

from __future__ import annotations

import os
import time

from agitrack.console import stdin_is_interactive
from agitrack.git import GitRepo
from agitrack.proc import pid_alive, terminate_pid

# How long a signalled process is given to go away before it is reported as a survivor. Matches
# daemons.stop_all: long enough for a daemon to finish a write, short enough that `agitrack stop`
# still feels like a command rather than a wait.
_GRACE_SECONDS = 5.0


def stop_everything(repo: GitRepo, *, assume_yes: bool = False) -> int:
    """Stop everything aGiTrack is running for ``repo``. Returns a process exit code.

    Exit 0 covers both "stopped it" and "nothing was running" — asking for a state that already
    holds is not an error. Exit 1 means something was asked to stop and would not.
    """
    from agitrack.metrics.collect import _abbreviate_home
    from agitrack.proxy.background import UNTRACKED_FROM_NOW_NOTE

    where = _abbreviate_home(str(repo.repo))
    acted: list[str] = []
    survivors: list[str] = []

    # Record the untracked stretch FIRST, and from here rather than only from the tracker's own
    # stop path: `stop` is the whole intention, and it means the same thing whether or not a
    # tracker happened to be up to receive it. The conversation almost always carries on in the
    # backend's own UI afterwards, writing into the very transcript the next tracker will read;
    # this is what keeps those turns out of the commit messages it writes (see
    # agitrack.tracking_gap). Marking is idempotent within one gap, so the tracker teardown
    # below marking again does not move the moment the gap began.
    _mark_untracked_from_now(repo)

    acted += _stop_background_tracker(repo, survivors)
    acted += _stop_sessions(repo, survivors, assume_yes=assume_yes)
    acted += _stop_views(repo, survivors)

    # ALWAYS, whatever was or was not running. The auto-start hooks are the thing that undoes a
    # stop, so removing them is not a tidy-up that only a tracker teardown owes: a tracker that
    # died without tearing down leaves them armed, and so does one that was never up while a
    # dashboard or an interactive session was. That last case used to skip the disarm entirely
    # (it hung off the "nothing was running" branch, which a running dashboard makes false), so
    # `agitrack stop` reported success on a repo whose OpenCode plugin and git hooks were still
    # armed to start a tracker on the next agent session or commit. Reported and reproduced.
    # `_disarm_tracking` is idempotent and answers "was anything armed", so calling it after the
    # tracker's own stop path already ran is silent.
    disarmed = _disarm(repo)

    if not acted and not survivors:
        print(f"aGiTrack is not running on {where}.")
    else:
        print(f"Stopped aGiTrack on {where}:")
        for line in acted:
            print(f"  • {line}")
        if disarmed:
            print("  • auto-start hooks (the git commit hook and the backends' session hooks)")
        for line in survivors:
            # Named rather than force-killed: a wedged daemon is a smaller problem than one killed
            # mid-write of its handshake or state.
            print(f"  ! {line}")
    if disarmed:
        print("Auto-start is off for this repo until you run `agitrack -b` (or `agitrack -i`) again.")
    print(UNTRACKED_FROM_NOW_NOTE)
    return 1 if survivors else 0


def _mark_untracked_from_now(repo: GitRepo) -> None:
    """Best-effort: a gap record that cannot be written must not stop the stop."""
    from agitrack import tracking_gap

    try:
        tracking_gap.mark_stopped(repo.repo)
    except Exception:
        pass


def _stop_background_tracker(repo: GitRepo, survivors: list[str]) -> list[str]:
    """Stop the headless tracker through its own stop path, so the teardown it does (final turn
    recorded, hooks disarmed, stop event logged) still happens."""
    from agitrack.proxy.background import _live_background_pid, stop_background

    if _live_background_pid(repo) is None:
        return []
    # stop_background prints its own report, which would read as a second, competing summary
    # next to ours. Silence it and keep one voice.
    import contextlib
    import io

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        code = stop_background(repo)
    if code != 0:
        survivors.append("the background tracker did not stop in time; it may still be shutting down")
        return []
    return ["background tracker"]


def _stop_sessions(repo: GitRepo, survivors: list[str], *, assume_yes: bool) -> list[str]:
    """Stop interactive TUI sessions running on this repo.

    An interactive session is a CONVERSATION someone may be in the middle of, which is why the
    machine-wide `--daemons stop` never touches one. `agitrack stop` does, because it is scoped to
    the repo and typed on purpose — but it says which session it is about to end and asks first,
    unless there is no one to ask or --yes was given. The session terminates the way Ctrl-G → quit
    does: SIGTERM, which its handler turns into an orderly shutdown."""
    from agitrack.daemons import deregister, list_running

    mine = os.getpid()
    sessions = [info for info in list_running(repo=repo.repo) if info.kind == "session" and info.pid != mine]
    if not sessions:
        return []
    if not assume_yes and stdin_is_interactive():
        plural = "s" if len(sessions) > 1 else ""
        pids = ", ".join(str(info.pid) for info in sessions)
        print(f"An interactive aGiTrack session{plural} is running on this repository (PID {pids}).")
        try:
            answer = input("Stop it too? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in {"n", "no"}:
            print("Left the interactive session running.")
            return []
    stopped: list[str] = []
    for info in sessions:
        terminate_pid(info.pid)
        if _wait_for_exit(info.pid):
            deregister(info.pid)
            stopped.append(f"interactive session (PID {info.pid})")
        else:
            survivors.append(f"interactive session (PID {info.pid}) is still running")
    return stopped


def _stop_views(repo: GitRepo, survivors: list[str]) -> list[str]:
    """Stop this repo's dashboard views.

    With one hub serving every repository, "stop the dashboard for this repo" means unmounting the
    repo from the hub rather than killing a process other repositories are still using — the hub
    itself only exits once nothing is left to serve. Older per-repo dashboard/backtrace daemons are
    still stopped the old way, so a stop issued right after an upgrade reaches them too."""
    from agitrack.daemons import deregister, list_running

    stopped: list[str] = []
    try:
        from agitrack.metrics.hub import unmount_repo

        if unmount_repo(repo.repo):
            stopped.append("dashboard (this repository is no longer served)")
    except ImportError:  # pragma: no cover - the hub is always importable in a real install
        pass
    mine = os.getpid()
    for info in list_running(repo=repo.repo):
        if info.kind not in {"dashboard", "backtrace"} or info.pid == mine:
            continue
        terminate_pid(info.pid)
        if _wait_for_exit(info.pid):
            deregister(info.pid)
            stopped.append(f"{info.function} (PID {info.pid})")
        else:
            survivors.append(f"{info.function} (PID {info.pid}) is still running")
    return stopped


def _disarm(repo: GitRepo) -> bool:
    """Remove every hook that would start tracking again: the git ``pre-commit`` auto-track hook
    and each backend's session-start hook (Claude and Codex settings entries, the OpenCode
    plugin). Returns whether anything was armed. Best-effort: a hook that will not come off must
    not fail the stop, and the recorded stop refuses an auto-start regardless."""
    try:
        from agitrack.proxy.background import _disarm_tracking

        return bool(_disarm_tracking(repo))
    except Exception:
        return False


def _wait_for_exit(pid: int) -> bool:
    deadline = time.monotonic() + _GRACE_SECONDS
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.05)
    return not pid_alive(pid)
