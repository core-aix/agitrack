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
from agitrack.commits.message import build_auto_fold_message
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
    terminate_pid(pid)
    for _ in range(100):  # up to ~10s for a clean shutdown
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    if pid_alive(pid):
        print(f"aGiTrack background tracker (PID {pid}) did not stop in time; it may still be shutting down.")
        return 1
    print("Stopped the aGiTrack background tracker.")
    return 0


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

    Reuses a daemon already running for this repo rather than spawning a duplicate. The daemon
    keeps running after the terminal closes; stop it with ``agitrack -b stop``."""
    running = _live_background_pid(repo)
    if running is not None:
        info = _read_handshake(repo) or {}
        print(
            f"\naGiTrack background tracker is already running on this repo (PID {running}, {info.get('mode', '?')})."
        )
        return 0
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
        # Tracking the agent committing its OWN work mid-turn. We snapshot HEAD the first time we
        # see a turn in-flight (incomplete); if the agent then `git commit`s within that turn, HEAD
        # advances from this baseline while the working tree ends clean — so when the turn finishes
        # we cover it onto that commit rather than dropping it (its code is already committed, so the
        # tree-delta gate would record nothing). Keyed per in-flight turn so an unrelated human
        # commit is never mistaken for a turn's work.
        self._inflight_turn_key: str | None = None
        self._inflight_baseline: str | None = None

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
        self._manual = ManualCommitTracker(self.repo, self.base_repo, self.state, debug=self._debug)

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
            self._observe_inflight_turn()
            self._process_once()
            if not self._manual_commits:
                self._auto_fold_pending()
        except Exception as error:
            self._debug(f"final process failed: {error!r}")
        self._manual.teardown()
        self._remove_handshake()
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
            from agitrack.proc import agitrack_invocation

            git_hooks.install_autotrack_precommit_hook(
                self.repo.hooks_dir(), invoke=agitrack_invocation(), repo_root=str(self.repo.repo), debug=self._debug
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
                inflight = self._observe_inflight_turn()  # snapshot a HEAD baseline while a turn runs
                self._process_once()  # records latently, or covers a turn the agent committed itself
                self._manual.service()
                if not self._manual_commits:
                    self._auto_fold_pending()
                if not inflight:
                    # No turn is in flight, so any just-completed turn has been processed this cycle;
                    # drop its baseline so it can NEVER be mistaken for a later (e.g. pure-Q&A) turn's
                    # work — the source of a spurious cover.
                    self._inflight_turn_key = None
                    self._inflight_baseline = None
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

    def _process_once(self) -> bool:
        """Export the user's active backend session and record any newly completed turns as
        latent commits. Returns True when a turn was recorded this cycle."""
        session = self._bare_session()
        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
        # Track whichever conversation is newest in the repo dir — the one the user is driving —
        # and follow an in-backend session switch. The per-conversation watermark keeps each
        # conversation's turns counted exactly once.
        engine.start_parse(
            session=session,
            discover_session_id_fn=lambda: self.backend.latest_session_id(self.repo.repo),
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
        )
        return bool(committed)

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
        """Record the completed turns. Normally each is a hidden latent commit (HEAD never moves;
        auto mode folds them via :meth:`_auto_fold_pending`). But if the agent committed this turn's
        OWN work mid-turn (see :meth:`_agent_committed_own_work`), the code is already in HEAD with a
        clean tree, so recording latently would find no delta and drop it. Instead we COVER the
        agent's commit(s) — ONE cover commit carrying the trace/metadata, with ``covered_commits``
        attributing their lines to AI — reusing the exact backend-commit cover path the proxy uses.
        Crucially this puts the turn's metadata in history exactly ONCE (the old two-step approach —
        a latent metadata commit plus a merge that embedded the same body — made it reachable twice
        and risked double-counting tokens)."""

        def on_commit_fn(sha, trace_text, _is_cover):
            self.events.emit("ai-change-detected", backend=backend, session=backend_session_id, model=model)
            self._start_commit_summary(sha, trace_text)

        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
        covered = self._agent_committed_own_work()
        if covered:
            # Cover the agent's own commit(s) instead of a latent record. Drop the baseline so the
            # same commits can never be covered a second time.
            self._inflight_baseline = None
            return engine.commit_turns(
                turns=turns,
                backend=backend,
                backend_session_id=backend_session_id,
                model=model,
                stage_untracked_fn=lambda _repo, _state: None,
                on_commit_fn=on_commit_fn,
                backend_commits=covered,
            )
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
    # Agent-committed-its-own-work-mid-turn tracking
    # ------------------------------------------------------------------

    def _observe_inflight_turn(self) -> bool:
        """Snapshot HEAD as a baseline the FIRST time we see the current turn in flight (its latest
        turn is incomplete). If the agent then commits its own work before finishing, HEAD advances
        from this baseline — the precise signal (unlike "HEAD moved recently") that lets us cover the
        turn onto that commit without ever mistaking an unrelated human commit for the turn's work.
        Keyed by the turn so a new turn re-baselines. Returns True while a turn is in flight (the
        loop keeps the baseline until the turn completes and is processed, then drops it)."""
        try:
            sid = self.backend.latest_session_id(self.repo.repo)
            exported = self.backend.export_session(self.repo.repo, sid) if sid else None
        except Exception:
            return self._inflight_baseline is not None
        turns = list(getattr(exported, "turns", []) or []) if exported else []
        if not turns or turns[-1].complete:
            return False  # nothing in flight; the loop clears the baseline after processing
        latest = turns[-1]
        key = f"{sid}:{latest.user_message_id or latest.assistant_message_id or len(turns)}"
        if key != self._inflight_turn_key:
            self._inflight_turn_key = key
            try:
                self._inflight_baseline = self.repo.rev_parse("HEAD")
            except Exception:
                self._inflight_baseline = None
        return True

    def _agent_committed_own_work(self) -> list[str]:
        """Full SHAs (oldest first) of the commit(s) the agent made ITSELF during the current turn —
        the commits on ``baseline..HEAD`` when the working tree is now clean — or ``[]`` otherwise.
        We watched the turn in flight and snapshotted ``_inflight_baseline`` at HEAD before the
        agent's commit, so an advance from it with a clean tree is precisely the agent committing its
        own code (never an unrelated human commit, which was not part of this observed turn). The
        caller covers these commits so the turn's trace/metadata land ONCE, with ``covered_commits``
        attributing their lines to AI — recording latently would find no tree delta and drop it."""
        if self._inflight_baseline is None:
            return []
        try:
            head = self.repo.rev_parse("HEAD")
            if head == self._inflight_baseline:
                return []  # no commit made this turn (e.g. a pure-Q&A turn)
            if self.repo.snapshot_worktree_tree() != self.repo.rev_parse("HEAD^{tree}"):
                return []  # dirty tree ⇒ the agent's edits are uncommitted; record latently instead
            return self.repo.log_shas(self._inflight_baseline, head)  # baseline..HEAD, oldest first
        except Exception as error:
            self._debug(f"committed-away detection failed: {error!r}")
            return []

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

        backend_class = OpenCodeBackend if self.state.backend == "opencode" else ClaudeBackend
        model = self.state.summarization_model
        if model is None and self.global_config is not None:
            model = self.global_config.summarization_model
        launch = self._backend_command or None
        return Summarizer(backend_class(summary_scratch_dir(), launch_command=launch), model=model)

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
