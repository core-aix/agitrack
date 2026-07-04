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

import signal
import threading
from typing import Any

from agitrack.backends.proxy_agents import make_proxy_agent
from agitrack.commits import ManualCommitTracker
from agitrack.commits.message import build_manual_squash_trailer
from agitrack.config import AgitrackState, GlobalConfig
from agitrack.git import GitRepo
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.proxy.session import Session


class BackgroundRunner:
    POLL_SECONDS = 3.0

    def __init__(
        self,
        repo: GitRepo,
        *,
        verbose: bool = False,
        backend: str | None = None,
        new_session: bool = False,
        manual_commits: bool = True,
        backend_command: list[str] | None = None,
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
        self._poll_seconds = poll_seconds if poll_seconds is not None else self.POLL_SECONDS
        self._lock = _lock
        self._stop = threading.Event()

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
        self._manual.setup()
        self._install_signal_handlers()
        mode = "manual (user-triggered) commits" if self._manual_commits else "auto commits"
        self._print(
            f"background tracker running for {self.state.backend} in {self.repo.repo} "
            f"({mode}, no worktree). Drive the agent from any UI; press Ctrl-C to stop."
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
        self._print("background tracker stopped.")

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
