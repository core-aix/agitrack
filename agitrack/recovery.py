"""Headless recovery of pending work left by a session that exited abruptly.

When an aGiTrack session is killed before it can finalize — e.g. the VSCode
window/terminal is closed mid-turn, or the process is otherwise SIGKILLed — the
agent's work survives on disk (uncommitted changes in the session worktree, plus
the backend transcript) but isn't committed or merged. The normal recovery is
*lazy*: the next ``agitrack`` launch reconciles it (``_reconcile_sessions_on_startup``).
This module makes that recovery *eager and standalone* so the editor extension
can run it the moment a session is closed, without waiting for a relaunch.

Policy (decided per the worktree's transcript, reusing the same commit pipeline a
live turn uses):

* The latest turn **finished** → commit its uncommitted changes (summarizing as
  configured), then merge into the base branch — skipping the merge on a conflict.
* The latest turn was **aborted / still in flight** → leave the changes untouched
  (never commit a half-finished turn) and flag the session for attention.
* Work that was already **committed but not merged** → merge it (skip on conflict),
  exactly as startup reconciliation does.

It runs only while no live aGiTrack holds the repo lock. The lock is an ``flock``,
which the kernel releases automatically when the holding process dies, so after an
abrupt exit the lock is free and recovery can take it; if a session is genuinely
still running, recovery acquires nothing and no-ops.

Scope: worktree sessions only. ``--no-worktree`` runs leave the agent's edits live
in the base working tree, intermixed with the user's own uncommitted changes, so
auto-committing them on close would be unsafe; those are left for the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agitrack.backends.proxy_agents import make_proxy_agent
from agitrack.commits import apply_summary_to_message, summary_metadata_lines
from agitrack.config import AgitrackState, GlobalConfig
from agitrack.git import GitRepo, RepoLock
from agitrack.git.worktree import WorktreeInfo, WorktreeManager
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.proxy.integration import IntegrationService
from agitrack.transcripts import turns_after

_DebugFn = Callable[[str], None]


@dataclass
class RecoveryReport:
    """Outcome of a recovery sweep, for logging / a one-line CLI summary."""

    recovered: list[str] = field(default_factory=list)  # finished turn committed
    integrated: list[str] = field(default_factory=list)  # merged into base + worktree removed
    flagged: list[str] = field(default_factory=list)  # left for manual attention
    skipped_busy: bool = False  # a live aGiTrack held the lock; nothing was done

    def did_work(self) -> bool:
        return bool(self.recovered or self.integrated or self.flagged)

    def summary(self) -> str:
        if self.skipped_busy:
            return "aGiTrack is already running on this repo; recovery skipped."
        if not self.did_work():
            return "Nothing to recover."
        parts: list[str] = []
        if self.recovered:
            parts.append(f"committed {len(self.recovered)} finished turn(s): {', '.join(self.recovered)}")
        if self.integrated:
            parts.append(f"integrated {len(self.integrated)} session(s): {', '.join(self.integrated)}")
        if self.flagged:
            parts.append(f"{len(self.flagged)} need attention: {', '.join(self.flagged)}")
        return "Recovery: " + "; ".join(parts) + "."


class RecoveryService:
    """Finalize pending work for a repo's session worktrees, headlessly."""

    def __init__(
        self,
        base_repo: GitRepo,
        global_config: GlobalConfig | None = None,
        *,
        debug_fn: _DebugFn | None = None,
    ) -> None:
        self.base_repo = base_repo
        self.global_config = global_config or GlobalConfig()
        self._debug = debug_fn or (lambda *_a, **_k: None)

    def recover(self) -> RecoveryReport:
        """Acquire the repo lock and recover every session worktree. No-op (and
        ``skipped_busy=True``) when a live aGiTrack already holds the lock."""
        lock = RepoLock(self.base_repo.repo / ".agitrack" / "lock")
        if not lock.acquire():
            return RecoveryReport(skipped_busy=True)
        try:
            return self._recover_locked()
        finally:
            lock.release()

    # ------------------------------------------------------------------

    def _recover_locked(self) -> RecoveryReport:
        report = RecoveryReport()
        base_branch = self.base_repo.current_branch()
        integration = IntegrationService(self.base_repo, base_branch)
        manager = WorktreeManager(self.base_repo)
        try:
            infos = manager.list()
        except Exception as error:
            self._debug(f"recover: worktree list failed: {error!r}")
            return report
        for info in infos:
            try:
                self._recover_one(info, integration, manager, report)
            except Exception as error:
                self._debug(f"recover: '{info.name}' failed: {error!r}")
                if info.name not in report.flagged:
                    report.flagged.append(info.name)
        try:
            integration.delete_orphan_merged_branches()
        except Exception as error:
            self._debug(f"recover: orphan-branch cleanup failed: {error!r}")
        return report

    def _recover_one(
        self,
        info: WorktreeInfo,
        integration: IntegrationService,
        manager: WorktreeManager,
        report: RecoveryReport,
    ) -> None:
        repo = GitRepo(info.path)
        if repo.merge_in_progress():
            report.flagged.append(info.name)  # mid-merge: leave for the user/agent
            return
        if repo.has_changes():
            # Uncommitted work: commit it ONLY if the latest turn finished.
            if self._commit_finished_turn(repo, info, integration, manager):
                report.recovered.append(info.name)
            else:
                # Aborted / in-flight / nothing committable — leave it untouched.
                report.flagged.append(info.name)
                return
        # The worktree is now clean (or was already): integrate committed work
        # into the base and remove the worktree. cleanup_stale_worktree skips
        # (returns False) on a dirty tree or a merge conflict.
        if integration.cleanup_stale_worktree(info, manager):
            report.integrated.append(info.name)
        elif info.name not in report.flagged:
            report.flagged.append(info.name)

    def _commit_finished_turn(
        self,
        repo: GitRepo,
        info: WorktreeInfo,
        integration: IntegrationService,
        manager: WorktreeManager,
    ) -> bool:
        """Commit the worktree's uncommitted changes iff the transcript's latest
        turn is complete. Returns True only when a commit was actually made."""
        state = AgitrackState(info.path, default_backend=self.global_config.default_backend)
        session_id = state.backend_session_id
        if not session_id:
            return False
        try:
            exported = make_proxy_agent(state.backend).export_session(repo.repo, session_id)
        except Exception as error:
            self._debug(f"recover: export failed for '{info.name}': {error!r}")
            return False
        if not exported:
            return False
        turns = turns_after(exported, state.last_backend_message_id)
        # The policy gate: a turn that is still in flight (or was interrupted) is
        # NOT committed. `complete` is False for an in-flight turn; a turn with no
        # final response (a bare abort) is excluded too.
        if not turns or not turns[-1].complete:
            return False
        complete_turns = [turn for turn in turns if turn.final_response]
        if not complete_turns:
            return False

        captured: dict = {}

        def stage_untracked_fn(target_repo: GitRepo, _state: AgitrackState) -> None:
            # A session worktree is an isolated sandbox, so the agent's new files
            # are staged wholesale (the proxy does the same in worktree mode).
            untracked = target_repo.untracked_files()
            if untracked:
                target_repo.stage_paths(untracked)

        def on_commit_fn(sha: str | None, trace: str, _is_cover: bool) -> None:
            captured["sha"] = sha
            captured["trace"] = trace

        def pre_commit_fn() -> None:
            # A worktree created detached has no branch; put the commit on a fresh
            # turn branch (agit/<backend>/<name>/tN) so it is a managed branch the
            # integration step can merge — exactly what the proxy does per turn.
            branch = "" if repo.is_detached() else repo.current_branch()
            integration.ensure_turn_branch(
                repo, info, integration.turn_from_branch(branch), manager, info.name, state.backend
            )

        committed = CommitEngine(repo, state, debug_fn=self._debug).commit_turns(
            turns=turns,
            backend=state.backend,
            backend_session_id=session_id,
            model=exported.model or state.model,
            stage_untracked_fn=stage_untracked_fn,
            pre_commit_fn=pre_commit_fn,
            on_commit_fn=on_commit_fn,
            # Build exactly the message a normal turn would — including the session
            # name — so a recovered commit is indistinguishable from one made live.
            session_name=info.name,
            accumulate_trace_only_on_commit=True,
        )
        if not committed:
            return False
        # Advance the watermark so a second recovery pass won't reprocess the turn.
        state.last_backend_message_id = complete_turns[-1].assistant_message_id
        self._summarize_and_amend(repo, state, captured.get("sha"), captured.get("trace"))
        return True

    def _summarize_and_amend(
        self,
        repo: GitRepo,
        state: AgitrackState,
        sha: str | None,
        trace_text: str | None,
    ) -> None:
        """Run the configured summarizer synchronously and fold the summary into
        the just-made commit (message amend while it is still HEAD, plus git
        notes), mirroring the proxy's async summary service. Best-effort: a
        failed summary never undoes the commit."""
        if not sha or not trace_text or not self._summarization_enabled(state):
            return
        summarizer = self._make_summarizer(state)
        if summarizer is None:
            return
        try:
            summary = summarizer.summarize_commit(trace=trace_text)
        except Exception as error:
            self._debug(f"recover: summarize failed: {error!r}")
            return
        metadata = summary_metadata_lines(
            model=summarizer.model or state.model,
            tokens_input=summarizer.tokens_input,
            tokens_output=summarizer.tokens_output,
            tokens_cache_read=summarizer.tokens_cache_read,
        )
        try:
            target = sha
            message = repo.commit_message("HEAD")
            amended = apply_summary_to_message(message, summary, summary_metadata=metadata)
            if amended != message and repo.rev_parse("HEAD") == repo.rev_parse(sha) and not repo.has_staged_changes():
                repo.amend_commit(amended)
                target = repo.rev_parse("HEAD")
            repo.notes_add(target, summary, namespace="agitrack/commit-summary")
            new_session_summary = summarizer.update_session_summary(
                current_summary=state.session_summary,
                trace=trace_text,
                commit_summary=summary,
            )
            state.session_summary = new_session_summary
            state.session_summary_commit = target
            repo.notes_add(target, new_session_summary, namespace="agitrack/session-summary")
        except Exception as error:
            self._debug(f"recover: applying summary failed: {error!r}")

    def _summarization_enabled(self, state: AgitrackState) -> bool:
        # The global config is the durable source of truth (mirrors the runner's
        # precedence); the per-session value is only a fallback for tests/configs
        # written before the global key existed.
        gc_enabled = getattr(self.global_config, "summarization_enabled", None)
        if gc_enabled is not None:
            return bool(gc_enabled)
        state_enabled = getattr(state, "summarization_enabled", None)
        return True if state_enabled is None else bool(state_enabled)

    def _make_summarizer(self, state: AgitrackState):
        from agitrack.backends.claude import ClaudeBackend
        from agitrack.backends.opencode import OpenCodeBackend
        from agitrack.summaries import Summarizer, summary_scratch_dir

        backend_class = OpenCodeBackend if state.backend == "opencode" else ClaudeBackend
        model = state.summarization_model
        if model is None and self.global_config is not None:
            model = self.global_config.summarization_model
        # The summarizer must run in a scratch cwd, never the worktree: its headless
        # backend calls record a session keyed by cwd, which would otherwise pollute
        # the repo's session list (issues #8/#56).
        return Summarizer(backend_class(summary_scratch_dir()), model=model)
