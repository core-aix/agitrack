"""Background (headless) tracker — run aGiTrack WITHOUT the interactive TUI.

Unlike proxy mode, aGiTrack does not spawn or drive the coding agent here: the user runs the
agent from whatever UI they like (its native CLI, an IDE extension, …), and this tracker watches
the agent's local session transcript and performs the same tracking the TUI would — recording
each completed turn, summarizing it, and installing the commit hooks that fold the interaction
trace and token metadata into commits. It ALWAYS runs without a worktree (it operates on the
current branch), with either manual (user-triggered) or auto (aGiTrack-triggered) commits.

This is the interactive-UI-agnostic tracker of issue #143: because it keys off the on-disk
session transcript rather than a PTY, it tracks a session no matter how the user drives the agent.

Both modes record each turn as a hidden latent commit on ``refs/agitrack/manual/<id>`` and rely
on a ``prepare-commit-msg`` hook to fold the pending turns' trace/metadata into the commit the
agent (or user) makes — a metadata-only cover commit is the fallback when the hook can't run.
The only difference is who triggers the commit: in **manual** mode the user does; in **auto**
mode aGiTrack folds the pending turns into a commit itself once the agent finishes a turn and
hasn't committed its own work. So in auto mode the agent's OWN commits fold via the hook, and the
cover is only a backup — exactly the requested behavior.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from agitrack.backends.proxy_agents import make_proxy_agent
from agitrack.commits import ManualCommitTracker
from agitrack.commits.message import build_auto_fold_message, is_fully_tracked_message, summary_metadata_lines
from agitrack.config import AgitrackState, GlobalConfig
from agitrack.events import EventLog, resolve_log_path
from agitrack.git import GitRepo
from agitrack.git import hooks as git_hooks
from agitrack.proc import detach_kwargs, pid_alive, terminate_pid
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.proxy.session import Session


def background_handshake_path(repo: GitRepo) -> Path:
    """Where a running background tracker records its pid, so `agitrack -b stop`/`status` can
    find it. Separate from the repo lock (which every mode shares) so stop/status target ONLY a
    background tracker, never a foreground TUI holding the same repo."""
    return repo.repo / ".agitrack" / "background.json"


def _read_handshake(repo: GitRepo) -> dict | None:
    try:
        data = json.loads(background_handshake_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _live_background_pid(repo: GitRepo) -> int | None:
    """The pid of a live background tracker on this repo, or None (also clears a stale file)."""
    info = _read_handshake(repo)
    pid = info.get("pid") if info else None
    if isinstance(pid, int) and pid_alive(pid):
        return pid
    if info is not None:  # stale handshake from a crashed tracker — clean it up
        try:
            background_handshake_path(repo).unlink()
        except OSError:
            pass
    return None


def background_status(repo: GitRepo) -> int:
    """Report whether a background tracker is running on this repo (`agitrack -b status`)."""
    pid = _live_background_pid(repo)
    if pid is None:
        print("No aGiTrack background tracker is running on this repo.")
    else:
        info = _read_handshake(repo) or {}
        mode = info.get("mode", "?")
        print(f"aGiTrack background tracker is running (PID {pid}, {mode}).")
    from agitrack.update.marker import update_reminder_line

    reminder = update_reminder_line(repo.repo)
    if reminder:
        print(reminder)
    return 0


def stop_background(repo: GitRepo) -> int:
    """Stop the background tracker running on this repo (`agitrack -b stop`). Sends SIGTERM and
    waits briefly for a clean shutdown (it records any final turn and removes its hooks)."""
    pid = _live_background_pid(repo)
    if pid is None:
        print("No aGiTrack background tracker is running on this repo.")
        return 0
    if not _terminate_and_wait(pid):
        print(f"aGiTrack background tracker (PID {pid}) did not stop in time; it may still be shutting down.")
        return 1
    print("Stopped the aGiTrack background tracker.")
    return 0


def replace_running_tracker(repo: GitRepo, *, owner_pid: int | None) -> bool:
    """``agitrack -b`` invoked while the repo lock is held: if the holder is a LIVE background
    tracker, stop it so this rerun replaces it — like ``-d`` and ``--backtrace``, rerunning
    the command must pick up updated aGiTrack code. Only a background tracker is replaced;
    when anything else holds the lock (an interactive TUI session) this returns False and the
    caller keeps refusing to start. True once the old tracker has shut down cleanly."""
    pid = _live_background_pid(repo)
    if pid is None or (owner_pid is not None and pid != owner_pid):
        return False
    if not _terminate_and_wait(pid):
        print(f"aGiTrack background tracker (PID {pid}) did not stop in time; run `agitrack -b stop` and try again.")
        return False
    print(f"Restarting the aGiTrack background tracker (was PID {pid}).")
    return True


def _terminate_and_wait(pid: int, *, timeout: float = 10.0) -> bool:
    """SIGTERM ``pid`` and wait (bounded) for its clean shutdown — the tracker records any
    final turn and removes its hooks on the way out. True once the process is gone."""
    terminate_pid(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.1)
    return not pid_alive(pid)


def background_log_path(repo: GitRepo) -> Path:
    """Where the detached daemon's stdout/stderr go (startup errors and per-turn notices land
    here — the daemon has no terminal). Mirrors the dashboard's ``dashboard.log``."""
    return repo.repo / ".agitrack" / "background.log"


def _flush_request_path(repo: GitRepo) -> Path:
    return repo.repo / ".agitrack" / "flush-request"


def _flush_done_path(repo: GitRepo) -> Path:
    return repo.repo / ".agitrack" / "flush-done"


def request_daemon_flush(repo: GitRepo, *, timeout: float = 5.0) -> bool:
    """Ask the running background daemon to record any pending COMPLETED turns and (re)render the
    fold trailer RIGHT NOW, then wait (bounded) for it to acknowledge.

    Called from the pre-commit hook (``precommit_sync``) when a daemon holds the repo lock: the
    daemon is the single writer, so the hook can't safely record turns itself, but the commit being
    made needs an up-to-date ``manual-pending-trailer`` for its ``prepare-commit-msg`` fold — not
    one lagging the daemon's poll. We write a unique nonce and spin until the daemon echoes it back
    (it services the request within ~0.1s). Best-effort: on timeout the commit simply proceeds with
    whatever the daemon last rendered. Returns True once acknowledged."""
    nonce = f"{os.getpid()}-{time.time()}"
    try:
        _flush_request_path(repo).write_text(nonce, encoding="utf-8")
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _flush_done_path(repo).read_text(encoding="utf-8").strip() == nonce:
                return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


def proxy_status_path(repo: GitRepo) -> Path:
    """Where the INTERACTIVE proxy records its pid + mode so `agitrack --status` can report it
    (the daemon uses ``background.json`; the shared repo lock only carries a pid, not the mode)."""
    return repo.repo / ".agitrack" / "session.json"


def write_proxy_status(repo: GitRepo, *, commits: str, worktree: bool) -> None:
    """Record the running interactive session's mode for `--status` (best-effort)."""
    try:
        path = proxy_status_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": os.getpid(), "mode": "interactive", "commits": commits, "worktree": bool(worktree)}),
            encoding="utf-8",
        )
    except OSError:
        pass


def clear_proxy_status(repo: GitRepo) -> None:
    try:
        path = proxy_status_path(repo)
        info = None
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = None
        if info is None or info.get("pid") == os.getpid():  # only clear our own record
            path.unlink()
    except OSError:
        pass


def _read_proxy_status(repo: GitRepo) -> dict | None:
    try:
        data = json.loads(proxy_status_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def repo_status(repo: GitRepo) -> int:
    """`agitrack --status` / `-s`: report whether aGiTrack is running for this repo and in which
    mode (interactive vs background, auto vs manual commit, worktree vs no-worktree)."""
    from agitrack.git import RepoLock
    from agitrack.update.marker import update_reminder_line

    def _commit_mode(handshake_mode: object) -> str:
        return "manual-commit" if isinstance(handshake_mode, str) and "manual" in handshake_mode else "auto-commit"

    bg_pid = _live_background_pid(repo)
    if bg_pid is not None:
        info = _read_handshake(repo) or {}
        print(
            f"aGiTrack is running in BACKGROUND mode (PID {bg_pid}): "
            f"{_commit_mode(info.get('mode'))}, no worktree, backend {info.get('backend', '?')}."
        )
    else:
        proxy = _read_proxy_status(repo)
        proxy_pid = proxy.get("pid") if proxy else None
        if proxy is not None and isinstance(proxy_pid, int) and pid_alive(proxy_pid):
            commits = "manual-commit" if proxy.get("commits") == "manual" else "auto-commit"
            worktree = "worktree" if proxy.get("worktree") else "no worktree"
            print(f"aGiTrack is running in INTERACTIVE mode (PID {proxy_pid}): {commits}, {worktree}.")
        else:
            owner = RepoLock(repo.repo / ".agitrack" / "lock").owner_pid()
            if isinstance(owner, int) and pid_alive(owner):
                print(f"aGiTrack is running for this repo (PID {owner}); mode details are unavailable.")
            else:
                print("aGiTrack is not running for this repo.")
    # Whether the persistent pre-commit hook will auto-start tracking on a commit (repo-scoped).
    try:
        config = GlobalConfig()
        config.load_repo_overlay(repo.repo)
        if config.autotrack_hook == "off":
            print("Auto-start on commit: off (`agitrack -b` or Ctrl-G → settings to enable).")
        else:
            last = read_background_mode(repo)
            mode = "manual-commit" if last else "auto-commit" if last is not None else "last-run"
            print(f"Auto-start on commit: on ({mode} mode; disable with `agitrack --remove-hooks`).")
    except Exception:
        pass
    reminder = update_reminder_line(repo.repo)
    if reminder:
        print(reminder)
    return 0


def _background_mode_path(repo: GitRepo) -> Path:
    return repo.repo / ".agitrack" / "background-mode"


def write_background_mode(repo: GitRepo, *, manual: bool) -> None:
    """Persist the commit mode of the LAST background run ("manual"/"auto"), so an auto-start on a
    later commit can resume the same mode. Survives the daemon stopping (unlike the handshake)."""
    try:
        path = _background_mode_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("manual" if manual else "auto", encoding="utf-8")
    except OSError:
        pass


def read_background_mode(repo: GitRepo) -> bool | None:
    """The last background run's commit mode as ``manual`` (True) / ``auto`` (False), or None when
    no run has been recorded yet."""
    try:
        text = _background_mode_path(repo).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if text == "manual":
        return True
    if text == "auto":
        return False
    return None


def _open_log(repo: GitRepo) -> Any:
    try:
        path = background_log_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("ab")
    except OSError:
        return subprocess.DEVNULL


def wait_for_handshake(repo: GitRepo, *, pid: int, timeout: float) -> dict | None:
    """Poll for the handshake the daemon child with ``pid`` writes once it starts tracking.

    Correlating on the child's pid means a stale record from an earlier daemon is never
    mistaken for this launch. Returns the record, or None if the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = _read_handshake(repo)
        if info is not None and info.get("pid") == pid:
            return info
        if not pid_alive(pid):  # child died during startup (e.g. lock lost, backend missing)
            return None
        time.sleep(0.05)
    return None


def spawn_background_daemon(repo: GitRepo, *, extra_args: list[str]) -> subprocess.Popen[bytes]:
    """Launch the detached background-tracker child and return its Popen handle.

    The child re-execs ``agitrack --background --background-serve`` in its own session
    (``detach_kwargs``) so it survives the launcher returning and the terminal closing, and
    is not hit by Ctrl-C in the launcher's terminal. Unlike the dashboard daemon there is NO
    owner-pid watchdog — a tracker must outlive whatever launched it (stop it with
    ``agitrack -b stop``). stdout/stderr go to a log file so a startup failure is recoverable."""
    cmd = [
        sys.executable,
        "-m",
        "agitrack",
        "--repo",
        str(repo.repo),
        "--background",
        "--background-serve",
        "--skip-privacy-ack",  # the interactive launcher already acknowledged it
        *extra_args,
    ]
    log = _open_log(repo)
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=str(repo.repo),
            env=dict(os.environ),
            **detach_kwargs(),
        )
    finally:
        if log is not subprocess.DEVNULL:
            log.close()  # the child holds its own dup of the fd


def start_background_daemon(repo: GitRepo, *, extra_args: list[str], timeout: float = 8.0) -> int:
    """`agitrack -b`: start the background tracker as a detached daemon and return to the shell.

    Restarts a daemon already running for this repo rather than leaving the old one in place —
    like ``-d`` and ``--backtrace``, re-running the command after an aGiTrack update must run
    the NEW code. The old tracker gets its clean SIGTERM shutdown first (it records any final
    turn and removes its hooks). The daemon keeps running after the terminal closes; stop it
    with ``agitrack -b stop``."""
    running = _live_background_pid(repo)
    if running is not None:
        info = _read_handshake(repo) or {}
        if not _terminate_and_wait(running):
            print(
                f"\naGiTrack background tracker (PID {running}) did not stop in time; "
                "run `agitrack -b stop` and try again."
            )
            return 1
        print(f"\nRestarting the aGiTrack background tracker (was PID {running}, {info.get('mode', '?')}).")
    proc = spawn_background_daemon(repo, extra_args=extra_args)
    record = wait_for_handshake(repo, pid=proc.pid, timeout=timeout)
    if record is None:
        print(f"\nThe aGiTrack background tracker did not start. See {background_log_path(repo)} for details.")
        return 1
    # Leading blank line separates this from the preceding startup messages (privacy ack, the
    # auto-start hook prompt) so each part of aGiTrack's start-up output reads as its own block.
    print(
        f"\naGiTrack background tracker daemon live (PID {record.get('pid')}, {record.get('mode', '?')}, no worktree).\n"
        "Drive the agent from any UI; it keeps tracking in the background. Stop it with `agitrack -b stop`."
    )
    return 0


def precommit_sync(repo: GitRepo, *, backend_command: list[str] | None = None) -> int:
    """Entry point of the persistent auto-track ``pre-commit`` hook. Best-effort; ALWAYS returns 0
    so it can never fail a commit.

    When aGiTrack is not already tracking this repo (its single-writer lock is free) and the AI has
    made changes since the last commit, this records the pending turns and renders the fold trailer
    so the interaction trace + token metadata land in the commit now being made — then, when
    ``background_autostart`` is set, starts the background daemon for future commits (else it prints
    a one-line reminder). A purely human commit (no pending AI turns) is left completely untouched:
    no trailer, no reminder, no daemon."""
    from agitrack.backends.setup import backend_installed
    from agitrack.git import RepoLock

    # Remind about an available update on EVERY commit (the marker is written by the background
    # tracker / interactive proxy; installing can't be automated). Visible in the git commit output.
    try:
        from agitrack.update.marker import update_reminder_line

        reminder = update_reminder_line(repo.repo)
        if reminder:
            print(f"aGiTrack: {reminder}", file=sys.stderr)
    except Exception:
        pass
    try:
        lock = RepoLock(repo.repo / ".agitrack" / "lock")
        if not lock.acquire():
            # A live tracker holds the lock — it is the single writer, so we must NOT record turns
            # here (that would race its state/refs and risk double-counting). But the commit being
            # made needs a FRESH fold trailer: if it's the BACKGROUND daemon, nudge it to record any
            # pending completed turns and re-render the trailer synchronously NOW, so this commit's
            # prepare-commit-msg hook folds the trace/metadata in instead of a trailer lagging the
            # daemon's poll (the bug where a commit racing the poll folded nothing). An interactive
            # TUI renders its own trailer as turns complete, so it needs no nudge.
            if _live_background_pid(repo) is not None:
                request_daemon_flush(repo)
            return 0
    except Exception:
        return 0
    config = GlobalConfig()
    config.load_repo_overlay(repo.repo)
    synced = False
    try:
        # manual_commits=True gives fold-into-the-user's-commit semantics: record the pending AI
        # turns latently and let THIS commit's prepare-commit-msg hook fold them in (no auto-commit).
        runner = BackgroundRunner(
            repo, manual_commits=True, backend_command=backend_command, _global_config=config, _lock=lock
        )
        if not backend_installed(runner.state.backend):
            return 0
        runner.state.ensure_local_ignore()  # git-ignore .agitrack/ before writing the trailer/ref
        runner._manual.setup()  # install the fold hooks (idempotent), reset a stale ref, render
        runner._process_once()  # parse the repo's own backend session, record NEW pending turns
        runner._manual.render_trailer()  # (re)render so the trailer carries the just-recorded turns
        synced = bool(runner._manual.pending_bodies())  # is there AI work to fold into this commit?
    except Exception:
        return 0
    finally:
        lock.release()  # release BEFORE spawning the daemon, which takes its own lock
    if not synced:
        return 0  # no AI work since the last commit ⇒ no footprint, no nag
    if config.autotrack_hook != "off" and _live_background_pid(repo) is None:
        # Auto-start the background tracker for the turns that FOLLOW (the current commit is already
        # handled by the trailer we just rendered — it stays the author's own manual commit). Use the
        # same commit mode as the last run; the *starting* commit is manual regardless.
        manual = read_background_mode(repo)
        if manual is None:
            manual = config.manual_commits
        spawn_background_daemon(repo, extra_args=["--manual-commits" if manual else "--auto-commit"])
        mode_label = "manual" if manual else "auto"
        print(
            f"aGiTrack: started automatically in {mode_label}-commit mode (same as last run) to keep tracking — "
            f"this commit stays your own. Stop it with `agitrack -b stop`; disable auto-start with "
            f"`agitrack --remove-hooks`.",
            file=sys.stderr,
        )
    return 0


class BackgroundRunner:
    POLL_SECONDS = 3.0

    def __init__(
        self,
        repo: GitRepo,
        *,
        verbose: bool = False,
        backend: str | None = None,
        new_session: bool = False,
        manual_commits: bool = False,  # background defaults to AUTO commits (like the interactive TUI)
        backend_command: list[str] | None = None,
        log_file: str | None = None,
        poll_seconds: float | None = None,
        _global_config: GlobalConfig | None = None,
        _state: AgitrackState | None = None,
        _lock=None,
    ) -> None:
        self.repo = repo
        self.base_repo = repo  # background mode is always no-worktree
        self.verbose = verbose
        self._manual_commits = manual_commits
        self._backend_command = list(backend_command or [])
        self.events = EventLog(resolve_log_path(log_file, repo.repo))
        self._poll_seconds = poll_seconds if poll_seconds is not None else self.POLL_SECONDS
        self._lock = _lock
        self._stop = threading.Event()
        # Periodic self-update check (never auto-applies — installing a newer aGiTrack can't be
        # fully automated). A found update is recorded to the shared marker so `-b status`, the
        # commit hook, and the dashboard can remind the user.
        self._last_update_check = 0.0
        self._update_thread: threading.Thread | None = None
        # Auto-fold waits (bounded) for the LLM summary to land so the commit's subject/lead is the
        # summary, not the raw prompt. Tracks the latent tip we're waiting on and since when.
        self._fold_wait_tip: str | None = None
        self._fold_wait_since: float | None = None
        # Summary worker threads keyed by the latent commit sha, so the fold can tell a summary
        # that's still computing from one that already FINISHED (and, if it produced no note, fold
        # now instead of waiting out the full summary_wait_seconds — e.g. the summarizer errored).
        self._summary_threads: dict[str, threading.Thread] = {}
        # Nonce of the last pre-commit flush request we serviced, so a repeated request (or a stale
        # request file across a restart) is handled at most once.
        self._last_flush_nonce: str | None = None
        # PERSISTENT tracking watermark: the HEAD up to which this daemon has accounted for AI work.
        # When a turn completes with a clean tree and HEAD has advanced past it, the new untracked
        # commits are the agent's own work (the agent/user committed it) and get COVERED with that
        # turn's trace/metadata. Persisting it (``.agitrack/tracked-head``) is what makes coverage
        # survive a daemon restart or the daemon being down at commit time — the fragile in-flight
        # HEAD snapshot it replaced was lost on every restart, leaving those commits untracked.
        self._tracked_head: str | None = None
        # Summary computed synchronously for the current cover commit's message (the daemon can't
        # amend HEAD to add it later), reused to also write the commit-summary git note.
        self._pending_cover_summary: str | None = None

        self.global_config = _global_config if _global_config is not None else GlobalConfig()
        if getattr(self.global_config, "repo_path", "set") is None:
            self.global_config.load_repo_overlay(repo.repo)
        self.state = (
            _state
            if _state is not None
            else AgitrackState(repo.repo, default_backend=backend or self.global_config.default_backend)
        )
        if backend and backend != self.state.backend:
            self.state.remember_backend_session()
            self.state.backend = backend
            self.global_config.default_backend = backend
            self.state.backend_session_id = self.state.stored_backend_session(backend)
            self.state.last_backend_message_id = None
        if new_session:
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            self.state.new_agitrack_session_id()
        self.backend = make_proxy_agent(self.state.backend)

        # Both modes record turns as latent commits and fold them via the prepare-commit-msg hook;
        # the tracker owns that machinery (shared with the proxy's manual mode).
        # Facts about the turn the agent is currently running, refreshed from every export (see
        # `_note_in_flight`). Lets the fold trailer attribute a commit the agent makes ITSELF
        # before that turn ends — the pre-commit flush re-exports first, so this is current.
        self._in_flight: dict | None = None
        self._manual = ManualCommitTracker(
            self.repo, self.base_repo, self.state, debug=self._debug, in_flight_fn=lambda: self._in_flight
        )

    # ------------------------------------------------------------------

    def _debug(self, message: str) -> None:
        if self.verbose:
            print(f"[agitrack:bg] {message}", flush=True)

    def _print(self, message: str) -> None:
        print(f"aGiTrack: {message}", flush=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> int:
        from agitrack.backends.setup import backend_installed

        if not backend_installed(self.state.backend):
            self._print(f"backend '{self.state.backend}' is not installed.")
            return 1
        self.state.ensure_local_ignore()  # git-ignore .agitrack/ before we write any state there
        write_background_mode(self.repo, manual=self._manual_commits)  # so an auto-start resumes this mode
        self._write_handshake()
        self._load_tracked_head()  # persistent coverage watermark (survives restarts)
        self._manual.setup()
        self._install_autotrack_hook()
        self._install_signal_handlers()
        mode = "manual (user-triggered) commits" if self._manual_commits else "auto commits"
        self.events.emit(
            "daemon-start",
            backend=self.state.backend,
            mode="manual" if self._manual_commits else "auto",
            repo=self.repo.repo,
        )
        self._print(
            f"background tracker running for {self.state.backend} in {self.repo.repo} "
            f"({mode}, no worktree). Drive the agent from any UI; stop it with `agitrack -b stop`."
        )
        try:
            self._loop()
        finally:
            self._teardown()
        return 0

    def _teardown(self) -> None:
        # Record any final completed turn (and, in auto mode, fold it) before stopping.
        try:
            self._process_once()
            if not self._manual_commits:
                self._auto_fold_pending()
        except Exception as error:
            self._debug(f"final process failed: {error!r}")
        self._manual.teardown()
        self._remove_handshake()
        from agitrack import daemons

        daemons.deregister()
        self.events.emit("daemon-stop", backend=self.state.backend)
        self._print("background tracker stopped.")

    def _write_handshake(self) -> None:
        # Record our pid so `agitrack -b stop`/`status` can target THIS background tracker
        # (as opposed to a foreground TUI, which shares the repo lock but not this file).
        try:
            path = background_handshake_path(self.repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "manual commits" if self._manual_commits else "auto commits"
            path.write_text(
                json.dumps(
                    {"pid": os.getpid(), "started_at": time.time(), "backend": self.state.backend, "mode": mode}
                ),
                encoding="utf-8",
            )
        except OSError as error:
            self._debug(f"handshake write failed: {error!r}")
        # Also record it in the global daemon registry, so `agitrack --daemons` lists it and a
        # self-update can restart it. Best-effort; never blocks the tracker.
        from agitrack import daemons

        daemons.register("background", self.repo.repo)

    def _remove_handshake(self) -> None:
        try:
            path = background_handshake_path(self.repo)
            info = _read_handshake(self.repo)
            # Only remove OUR handshake (guard against a race where another tracker rewrote it).
            if info and info.get("pid") == os.getpid():
                path.unlink()
        except OSError:
            pass

    def _maybe_check_update(self) -> None:
        """Periodically check for a newer aGiTrack (throttled to update_check_seconds, on a worker
        thread so the poll never blocks on the network). NEVER auto-applies — it records the result
        to the shared marker so `-b status`, the commit hook, and the dashboard remind the user."""
        if not getattr(self.global_config, "check_for_updates", True):
            return
        if self._update_thread is not None and self._update_thread.is_alive():
            return
        try:
            interval = float(self.global_config.timings.get("update_check_seconds", 300.0))
        except Exception:
            interval = 300.0
        now = time.monotonic()
        if self._last_update_check and (now - self._last_update_check) < interval:
            return
        self._last_update_check = now
        self._update_thread = threading.Thread(target=self._run_update_check, name="agit-bg-update", daemon=True)
        self._update_thread.start()

    def _run_update_check(self) -> None:
        try:
            from agitrack.update.marker import clear_update_marker, write_update_marker
            from agitrack.update.updater import Updater

            status = Updater().check()
            if not status.ok:
                return
            if status.available:
                write_update_marker(
                    self.repo.repo, current=status.current, latest=status.latest, message=status.message
                )
                self.events.emit("update-available", current=status.current, latest=status.latest)
                self._print(f"update available: {status.current} → {status.latest}. Run `agitrack` to update.")
            else:
                clear_update_marker(self.repo.repo)  # up to date now — drop a stale marker
        except Exception as error:
            self._debug(f"update check failed: {error!r}")

    def _install_autotrack_hook(self) -> None:
        """Install the PERSISTENT auto-track pre-commit hook so a commit made after this daemon
        stops (e.g. a reboot) still records its AI work. Deliberately NOT removed on teardown — it
        lives on so tracking survives aGiTrack not running (that hook re-installs the fold hooks and
        re-starts the daemon on demand). Gated by the repo-scoped ``autotrack_hook`` setting: when
        the user chose "off" (via the `agitrack -b` prompt) it is NOT installed, and any existing
        one is removed. No-op when a custom core.hooksPath makes install impossible."""
        try:
            if self.repo.core_hooks_path():
                self._debug("autotrack hook skipped: core.hooksPath is set")
                return
            if getattr(self.global_config, "autotrack_hook", "auto") == "off":
                git_hooks.remove_autotrack_precommit_hook(self.repo.hooks_dir(), debug=self._debug)
                self._debug("autotrack hook removed (autotrack_hook=off)")
                return
            from agitrack import __version__
            from agitrack.proc import agitrack_invocation

            # Stamp the running aGiTrack version so a later start can detect a stale hook SCHEMA and,
            # if this version is newer, remove the previously installed hook and re-add the current
            # one (handled inside install_autotrack_precommit_hook).
            git_hooks.install_autotrack_precommit_hook(
                self.repo.hooks_dir(),
                invoke=agitrack_invocation(),
                repo_root=str(self.repo.repo),
                version=__version__,
                debug=self._debug,
            )
        except Exception as error:
            self._debug(f"autotrack hook install failed: {error!r}")

    def _install_signal_handlers(self) -> None:
        def handler(_signum, _frame):
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - not on the main thread
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_once()  # records latently, or covers commits the agent made itself
                self._manual.service()
                if not self._manual_commits:
                    self._auto_fold_pending()
                self._maybe_check_update()
            except Exception as error:  # never let one bad cycle kill the tracker
                self._debug(f"cycle error: {error!r}")
            self._wait_with_flush(self._poll_seconds)

    def _wait_with_flush(self, seconds: float) -> None:
        """Sleep up to ``seconds`` between poll cycles, but stay responsive to a pre-commit flush
        request from the auto-track hook: service it within ~0.1s so a commit isn't held waiting for
        the next full poll. Wakes immediately when stopping."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            self._service_flush_requests()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(0.1, remaining))

    def _service_flush_requests(self) -> None:
        """Handle a pre-commit flush request (see :func:`request_daemon_flush`): record any pending
        completed turns and (re)render the fold trailer synchronously, then echo the nonce back so
        the waiting hook can proceed. Runs on the daemon's own loop thread — the single writer — so
        it never races the poll. At pre-commit time the working tree still holds the about-to-be-
        committed changes, so a just-finished turn records cleanly and folds into THAT commit."""
        try:
            nonce = _flush_request_path(self.repo).read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not nonce or nonce == self._last_flush_nonce:
            return
        try:
            self._process_once()
            self._manual.render_trailer()
        except Exception as error:
            self._debug(f"flush failed: {error!r}")
        self._last_flush_nonce = nonce
        try:
            _flush_done_path(self.repo).write_text(nonce, encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Turn processing (reuses the proxy's CommitEngine so accounting is identical)
    # ------------------------------------------------------------------

    def _bare_session(self) -> Any:
        # Session sets its per-session fields dynamically from FIELDS, so it is used untyped here
        # (as CommitEngine does), letting the attribute assignments below type-check.
        session: Any = Session.bare()
        session.repo = self.repo
        session.state = self.state
        session.backend = self.backend
        session.worktree = None  # background mode never uses a worktree
        session.name = None
        return session

    def _tracked_session_id(self) -> str | None:
        """The conversation to export this cycle: the newest one a HUMAN is driving here.

        The interactive proxy gets this for free — it spawns the backend itself and pins that
        session (``ProxyRunner._discover_spawned_session``), so it tracks exactly one
        conversation no matter what else runs in the directory. The daemon spawns nothing and
        has to choose, and "newest transcript in the repo dir" is the wrong rule as soon as the
        tracked agent fans work out to SDK workers: each worker writes its own transcript into
        that same directory, so every one of them gets adopted as the user's next turn — one
        commit per worker, each carrying that worker's interaction trace, each snapshotting
        whatever half-finished output files happened to exist at that moment.
        ``list_sessions`` already drops programmatic transcripts, so the newest survivor is the
        real conversation.

        Returns None when nobody is driving an agent here. The caller must then skip the cycle
        rather than let the pinned id stand in: that id may itself be a programmatic session
        adopted before this filter existed, and a still-running one would keep feeding the
        daemon "turns" the user never asked for.
        """
        try:
            return self.backend.latest_session_id(self.repo.repo)
        except Exception as error:
            self._debug(f"tracked session lookup failed: {error!r}")
            return None

    def _process_once(self) -> bool:
        """Export the user's active backend session and record any newly completed turns as
        latent commits. Returns True when a turn was recorded this cycle."""
        session_id = self._tracked_session_id()
        if session_id is None:
            self._debug("no human-driven session in this repo; nothing to export")
            return False
        session = self._bare_session()
        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
        # Follow an in-backend session switch (the user starting or resuming a conversation)
        # while ignoring programmatic ones. The per-conversation watermark keeps each
        # conversation's turns counted exactly once.
        engine.start_parse(
            session=session,
            discover_session_id_fn=lambda: session_id,
            debug_fn=self._debug,
        )
        thread = session.agent_parse_thread
        if thread is not None:
            thread.join(timeout=30)
            if thread.is_alive():
                self._debug("parse worker still running; will retry next cycle")
                return False
        committed, _ = engine.finish_parse_if_ready(
            session=session,
            quiet=True,
            prompt_untracked=True,
            require_complete=True,
            awaited_followups=[],
            agent_is_active_fn=lambda: False,
            debug_fn=self._debug,
            note_session_change_fn=lambda _sid: None,
            mirror_fn=lambda _sid: None,
            commit_fn=self._record_turns,
            note_in_flight_fn=self._note_in_flight,
        )
        return bool(committed)

    def _note_in_flight(self, facts: dict | None) -> None:
        """Remember (or clear) the running turn's facts. The pre-commit flush re-renders the
        trailer right after ``_process_once``, so this is current at commit time; re-rendering on
        a change as well keeps attribution working even if that nudge never lands (a removed
        pre-commit hook, a ``core.hooksPath`` that skips it)."""
        changed = self._in_flight != facts
        self._in_flight = facts
        if changed:
            self._manual.render_trailer()

    def _record_turns(
        self,
        *,
        turns,
        backend: str,
        backend_session_id: str | None,
        model: str | None,
        quiet: bool,
        prompt_untracked: bool = True,
    ) -> bool:
        """Record the completed turns. When the agent still has UNCOMMITTED edits in the working tree
        this records a hidden latent commit (HEAD never moves; auto mode folds it via
        :meth:`_auto_fold_pending`). But when the agent (or user) already COMMITTED this turn's work —
        so the tree is clean and HEAD has advanced past our watermark (see
        :meth:`_agent_committed_own_work`) — recording latently would find no delta and silently drop
        the turn. Instead we COVER those commit(s): ONE cover commit carrying the trace/metadata with
        ``covered_commits`` attributing their lines to AI, reusing the exact backend-commit cover path
        the proxy uses (metadata lands in history exactly once)."""

        def on_commit_fn(sha, trace_text, _is_cover):
            self.events.emit("ai-change-detected", backend=backend, session=backend_session_id, model=model)
            self._start_commit_summary(sha, trace_text)

        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
        covered = self._agent_committed_own_work()
        if covered:
            # The cover commit is created immediately and the daemon never amends HEAD, so — unlike
            # the async note flow for other commits — its message must LEAD with the summary already.
            # Summarize synchronously via summarize_fn, and write the same summary as a git note.
            self._pending_cover_summary = None

            def on_commit_cover(sha, _trace_text, _is_cover):
                self.events.emit("ai-change-detected", backend=backend, session=backend_session_id, model=model)
                if self._pending_cover_summary:
                    try:
                        self.repo.notes_add(
                            self.repo.rev_parse(sha), self._pending_cover_summary, namespace="agitrack/commit-summary"
                        )
                    except Exception as error:
                        self._debug(f"cover summary note failed: {error!r}")

            result = engine.commit_turns(
                turns=turns,
                backend=backend,
                backend_session_id=backend_session_id,
                model=model,
                stage_untracked_fn=lambda _repo, _state: None,
                on_commit_fn=on_commit_cover,
                backend_commits=covered,
                summarize_fn=self._summarize_for_message,
            )
            if result:
                self._set_tracked_head(self.repo.rev_parse("HEAD"))  # everything up to the cover is accounted for
            return result
        return engine.commit_turns(
            turns=turns,
            backend=backend,
            backend_session_id=backend_session_id,
            model=model,
            stage_untracked_fn=lambda _repo, _state: None,  # latent path never stages
            on_commit_fn=on_commit_fn,
            manual_gate_fn=self._manual.gate,
            manual_record_fn=self._manual.record,
        )

    # ------------------------------------------------------------------
    # Persistent coverage watermark (agent/user committed the turn's own work)
    # ------------------------------------------------------------------

    def _tracked_head_path(self) -> Path:
        return self.repo.repo / ".agitrack" / "tracked-head"

    def _load_tracked_head(self) -> None:
        """Load the persisted coverage watermark, or initialize it to the current HEAD on a repo we
        have never tracked (so pre-existing history is NEVER retroactively attributed to AI). Persisted
        so a restart — or the daemon being down when a commit was made — still covers those commits."""
        try:
            saved = self._tracked_head_path().read_text(encoding="utf-8").strip()
        except OSError:
            saved = ""
        try:
            if saved and self.repo.rev_parse(saved):
                self._tracked_head = self.repo.rev_parse(saved)
                return
        except Exception:
            pass
        try:
            self._set_tracked_head(self.repo.rev_parse("HEAD"))
        except Exception:
            self._tracked_head = None

    def _set_tracked_head(self, sha: str | None) -> None:
        self._tracked_head = sha
        try:
            if sha:
                self.state.ensure_local_ignore()  # keep .agitrack/ ignored before writing into it
                self._tracked_head_path().write_text(sha + "\n", encoding="utf-8")
        except OSError as error:
            self._debug(f"tracked-head write failed: {error!r}")

    def _is_agitrack_tracked(self, sha: str) -> bool:
        """True when a commit already carries COMPLETE aGiTrack tracking (its own metadata, or an
        aGiTrack cover/merge), so it must not be covered again. Keeps the daemon's own commits and
        hook-folded user commits out of a cover's ``covered_commits``.

        A commit carrying only an IN-FLIGHT block is deliberately NOT tracked: it was stamped
        mid-turn with attribution alone, so the completed turn still owes it a trace and token
        counts and must cover it."""
        try:
            body = self.repo.commit_message(sha) or ""
        except Exception:
            return False
        return is_fully_tracked_message(body) or body.lstrip().startswith("<aGiTrack")

    def _agent_committed_own_work(self) -> list[str]:
        """Full SHAs (oldest first) of the commit(s) to cover for a just-completed turn — the
        UNTRACKED commits on ``tracked_head..HEAD`` when the working tree is clean — or ``[]``.

        The persistent watermark (not a per-turn in-flight snapshot) is what makes this survive a
        daemon restart or the daemon being down at commit time: whenever a turn finishes and HEAD has
        advanced past the watermark with the tree clean, the agent/user committed the turn's work
        themselves, so those commits are covered and the watermark advances. Commits that already
        carry aGiTrack tracking are skipped (and the watermark still advances past them), so the
        daemon's own commits and hook-folded user commits are never re-covered. The one trade-off,
        deliberately chosen for reliable coverage over the old "miss it entirely" behavior: a purely
        human commit that happens to sit between two AI turns is attributed to the later turn."""
        if self._tracked_head is None:
            return []
        try:
            head = self.repo.rev_parse("HEAD")
            if head == self._tracked_head:
                return []  # no new commit since we last accounted (e.g. a pure-Q&A turn)
            if self.repo.snapshot_worktree_tree() != self.repo.rev_parse("HEAD^{tree}"):
                return []  # dirty tree ⇒ the agent's edits are uncommitted; record latently instead
            commits = self.repo.log_shas(self._tracked_head, head)  # tracked_head..HEAD, oldest first
        except Exception as error:
            self._debug(f"committed-away detection failed: {error!r}")
            return []
        untracked = [sha for sha in commits if not self._is_agitrack_tracked(sha)]
        if not untracked or untracked[-1] != head:
            # Nothing new to cover, or a tracked commit sits at HEAD (can't cover onto it) — just
            # advance the watermark so we don't reconsider these commits every cycle.
            self._set_tracked_head(head)
            return []
        return untracked

    # ------------------------------------------------------------------
    # Auto mode: fold the pending latent turns into a real commit ourselves
    # ------------------------------------------------------------------

    def _auto_fold_pending(self) -> None:
        """Auto mode: aGiTrack commits the pending latent turns itself — folding their full
        trace/metadata — so the user doesn't have to. If the working tree is already clean the
        agent (or user) committed its own work, in which case the prepare-commit-msg fold hook
        folded the tracking into THAT commit (cover being only the fallback), and there is nothing
        for us to do. The tree is snapshotted with the same scaffolding filter the latent path
        uses, so ``.agitrack`` churn never counts as work."""
        ref = self._manual.ref()
        tip = self.repo.ref_sha(ref)
        if not tip:
            return
        try:
            # Clean working tree vs HEAD ⇒ the agent (or user) already committed its work, and the
            # prepare-commit-msg fold hook folded the tracking into THAT commit — nothing to do.
            if self.repo.snapshot_worktree_tree() == self.repo.rev_parse("HEAD^{tree}"):
                return
        except Exception:
            return
        if self._manual.pending_count() == 0:
            return
        # Let the LLM summary land first so the commit is summarized (subject + lead paragraph),
        # not left with the raw prompt. Bounded — after summary_wait_seconds we fold anyway. Unlike
        # the interactive proxy the daemon never amends HEAD (the user may be committing), so the
        # summary must be in BEFORE the commit, not amended in after.
        if not self._fold_summary_ready(tip):
            return  # retry next cycle
        bodies = self._manual.pending_bodies()  # re-read: any arrived summaries are now applied
        message = build_auto_fold_message(bodies)
        if not message:
            return
        try:
            self.repo.add_tracked()
            declined = set(self.state.declined_untracked())
            self.repo.stage_paths([p for p in self.repo.untracked_entries() if p not in declined])
            if not self.repo.has_staged_changes():
                return
            # The message already carries the folded metadata, so the prepare-commit-msg hook's
            # idempotency check skips re-appending it; the post-commit hook resets the latent ref.
            self.repo.commit(message)
            self._manual.reset_stale_ref()
            self._manual.last_head = self.repo.rev_parse("HEAD")
            self._set_tracked_head(self.repo.rev_parse("HEAD"))  # our own fold commit is accounted for
            self._manual.render_trailer()
            self._fold_wait_tip = None
            self._fold_wait_since = None
            self.events.emit("commit", sha=self.repo.rev_parse("HEAD")[:12], type="agent", backend=self.state.backend)
            self._print("committed agent turn(s).")
        except Exception as error:
            self._debug(f"auto fold failed: {error!r}")

    def _fold_summary_ready(self, tip: str) -> bool:
        """True when the auto-fold may proceed: summarization off, the tip's summary note has
        landed, the summary worker already finished WITHOUT one (it errored — don't wait it out), or
        the bounded wait (``summary_wait_seconds``) elapsed. Returns False only while a summary is
        genuinely still computing and within the wait window (so the caller retries next cycle)."""
        if not self._summarization_enabled():
            return True
        try:
            note = self.repo.notes_show(tip, namespace="agitrack/commit-summary")
            if note and note.strip():
                return True
        except Exception:
            return True  # can't read notes ⇒ don't block folding
        worker = self._summary_threads.get(tip)
        if worker is not None and not worker.is_alive():
            # The summary worker for this turn finished but left no note (the summarizer errored or
            # returned nothing). Fold now rather than waiting out the full deadline for a summary
            # that will never arrive — the commit just keeps its raw-prompt subject.
            return True
        now = time.monotonic()
        if self._fold_wait_tip != tip:  # a new tip to wait on — restart the clock
            self._fold_wait_tip = tip
            self._fold_wait_since = now
            return False
        if self._fold_wait_since is None:
            self._fold_wait_since = now
            return False
        try:
            deadline = float(self.global_config.timings.get("summary_wait_seconds", 45.0))
        except Exception:
            deadline = 45.0
        return (now - self._fold_wait_since) >= deadline

    # ------------------------------------------------------------------
    # Summaries (best-effort, written as git notes so the fold picks them up)
    # ------------------------------------------------------------------

    def _summarization_enabled(self) -> bool:
        # The GLOBAL config (with repo overlay) is the durable source of truth and wins — matching
        # the proxy (ProxyRunner._summarization_enabled). The per-repo AgitrackState always defaults
        # "on", so it must NOT shadow a global/repo `summarization_enabled: false` or the toggle
        # would never take effect in background mode. Fall back to state only with no global config.
        gc_enabled = getattr(self.global_config, "summarization_enabled", None)
        if gc_enabled is not None:
            return bool(gc_enabled)
        return bool(getattr(self.state, "summarization_enabled", True))

    def _make_summarizer(self):
        if not self._summarization_enabled():
            return None
        from agitrack.backends.claude import ClaudeBackend
        from agitrack.backends.opencode import OpenCodeBackend
        from agitrack.summaries import Summarizer, summary_scratch_dir
        from agitrack.summaries.model_select import compatible_summarization_model

        backend_name = "opencode" if self.state.backend == "opencode" else "claude"
        backend_class = OpenCodeBackend if backend_name == "opencode" else ClaudeBackend
        model = self.state.summarization_model
        if model is None and self.global_config is not None:
            model = self.global_config.summarization_model
        # A summarization_model configured for a different backend (e.g. a Claude id under an
        # OpenCode session) is invalid there and fails every summary — drop it for the default.
        model = compatible_summarization_model(backend_name, model)
        launch = self._backend_command or None
        return Summarizer(backend_class(summary_scratch_dir(), launch_command=launch), model=model)

    def _summarize_for_message(self, trace_text: str) -> tuple[str, list[str]] | None:
        """Summarize a cover's interaction trace SYNCHRONOUSLY, returning ``(summary, metadata)`` so
        the cover's commit message can LEAD with the summary. The daemon never amends HEAD, so unlike
        the proxy it can't attach the summary after committing — it must be in the message up front
        (the auto-fold path likewise waits for the summary before committing). Best-effort: returns
        None when summaries are off or the summarizer fails, leaving the raw prompt-led message. The
        summary is cached so :meth:`_record_turns` can also write it as the commit-summary note."""
        summarizer = self._make_summarizer()
        if summarizer is None:
            return None
        try:
            summary = summarizer.summarize_commit(trace=trace_text)
        except Exception as error:
            self._debug(f"cover summary failed: {error!r}")
            return None
        if not summary or not summary.strip():
            return None
        self._pending_cover_summary = summary
        meta = summary_metadata_lines(
            model=summarizer.model,
            tokens_input=summarizer.tokens_input,
            tokens_output=summarizer.tokens_output,
            tokens_cache_read=summarizer.tokens_cache_read,
        )
        return summary, meta

    def _start_commit_summary(self, sha: str, trace_text: str) -> None:
        summarizer = self._make_summarizer()
        if summarizer is None:
            return
        try:
            full_sha = self.repo.rev_parse(sha)
        except Exception:
            return

        def worker() -> None:
            try:
                summary = summarizer.summarize_commit(trace=trace_text)
            except Exception as error:
                self._debug(f"summary failed for {sha}: {error!r}")
                return
            if not summary or not summary.strip():
                return
            try:
                # Record as a git note; the fold (pending_bodies) and the dashboard both read it.
                # Never amend HEAD here — the tracker runs while the user may be committing, so
                # touching HEAD could race their commit.
                self.repo.notes_add(full_sha, summary, namespace="agitrack/commit-summary")
                self._manual.render_trailer()
            except Exception as error:
                self._debug(f"summary note failed for {sha}: {error!r}")

        thread = threading.Thread(target=worker, name="agit-bg-summary", daemon=True)
        self._summary_threads[full_sha] = thread
        thread.start()
