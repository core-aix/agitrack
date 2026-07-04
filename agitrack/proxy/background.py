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
from agitrack.commits.message import build_manual_squash_trailer
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
        print(f"aGiTrack background tracker is already running on this repo (PID {running}, {info.get('mode', '?')}).")
        return 0
    proc = spawn_background_daemon(repo, extra_args=extra_args)
    record = wait_for_handshake(repo, pid=proc.pid, timeout=timeout)
    if record is None:
        print(f"The aGiTrack background tracker did not start. See {background_log_path(repo)} for details.")
        return 1
    print(
        f"aGiTrack background tracker daemon live (PID {record.get('pid')}, {record.get('mode', '?')}, no worktree).\n"
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
            return 0  # a live tracker (TUI or daemon) already folds this commit — nothing to do
    except Exception:
        return 0
    config = GlobalConfig()
    config.load_repo_overlay(repo.repo)
    manual_mode = config.manual_commits
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
    if config.background_autostart and _live_background_pid(repo) is None:
        # Start the persistent tracker for FUTURE commits (fire-and-forget — the current commit is
        # already handled by the trailer we just rendered). It runs in the user's configured mode.
        spawn_background_daemon(repo, extra_args=["--manual-commits" if manual_mode else "--auto-commit"])
        print(
            "aGiTrack: auto-started background tracking for this repo (stop with `agitrack -b stop`).",
            file=sys.stderr,
        )
    else:
        print(
            "aGiTrack: tracked this commit's AI work. Start `agitrack -b` for continuous background tracking.",
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
        re-starts the daemon on demand). No-op when a custom core.hooksPath makes install impossible."""
        try:
            if self.repo.core_hooks_path():
                self._debug("autotrack hook skipped: core.hooksPath is set")
                return
            git_hooks.install_autotrack_precommit_hook(
                self.repo.hooks_dir(), python_exe=sys.executable, repo_root=str(self.repo.repo), debug=self._debug
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
                self._process_once()
                self._manual.service()
                if not self._manual_commits:
                    self._auto_fold_pending()
                self._maybe_check_update()
            except Exception as error:  # never let one bad cycle kill the tracker
                self._debug(f"cycle error: {error!r}")
            self._stop.wait(self._poll_seconds)

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
        """Record the completed turns as hidden latent commits (HEAD never moves); summarize
        each. Both manual and auto mode record latently — auto mode additionally folds them into
        a real commit itself (see :meth:`_auto_fold_pending`)."""

        def on_commit_fn(sha, trace_text, _is_cover):
            self.events.emit("ai-change-detected", backend=backend, session=backend_session_id, model=model)
            self._start_commit_summary(sha, trace_text)

        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
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
        bodies = self._manual.pending_bodies()
        if not bodies:
            return
        message = "<aGiTrack> commit agent turns\n\n" + build_manual_squash_trailer(
            agitrack_session_id=self.state.session_id, latent_bodies=bodies
        )
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
            self.events.emit("commit", sha=self.repo.rev_parse("HEAD")[:12], type="agent", backend=self.state.backend)
            self._print("committed agent turn(s).")
        except Exception as error:
            self._debug(f"auto fold failed: {error!r}")

    # ------------------------------------------------------------------
    # Summaries (best-effort, written as git notes so the fold picks them up)
    # ------------------------------------------------------------------

    def _summarization_enabled(self) -> bool:
        value = self.state.summarization_enabled
        if value is None and self.global_config is not None:
            value = self.global_config.summarization_enabled
        return bool(value)

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

        threading.Thread(target=worker, name="agit-bg-summary", daemon=True).start()
