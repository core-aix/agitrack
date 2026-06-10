"""Branch/merge/integration policy for aGiT worktree sessions (#29, P5).

:class:`IntegrationService` encapsulates the git operations that manage the
lifecycle of session turn branches and their integration into the base branch.
It is deliberately UI-free: every method returns a plain outcome (a string or
dataclass) and the ProxyRunner renders messages / popups in response.

The service takes all its inputs as explicit parameters (``base_repo``,
``base_branch``, per-call ``repo`` / ``name`` / ``worktree``) so it is safe
to call from inside temp-swap windows — it never reads ``runner.active``
implicitly.

Merge-context state machine
---------------------------

The production code uses at most **one** active merge resolution at a time per
session (the ``merge_ctx`` field on a ``Session`` object).  The abandoned
:mod:`agit.merge_queue` module modelled a *queue* of pending merges serialised
through a MergeCoordinator — a design that does not map onto the existing
single-resolution-at-a-time flow:

* MergeCoordinator queues sessions and processes them one at a time, while the
  production code resolves each session's merge independently and concurrently
  (background sessions are serviced via ``_with_session`` from the event loop).
* The ``prompt_sent_at`` timing in the production flow (an Enter keystroke
  arrives asynchronously after the text is injected) has no counterpart in the
  MergeParticipant protocol.
* Adopting the queue model would change *when* merges start and finalize, which
  is an observable behavior change outside the allowed scope (P5 only permits
  formalising the state machine).

Decision: ``agit/merge_queue.py`` and its tests are deleted. Instead, the
ad-hoc ``merge_ctx`` dict is replaced here by a :class:`MergeContext`
dataclass with an explicit :class:`MergePhase` enum, preserving all existing
timing semantics byte-for-byte.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from agit.commit_message import build_agent_merge_message
from agit.git import GitRepo
from agit.worktree import WorktreeManager

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Merge-context state machine
# ---------------------------------------------------------------------------

class MergePhase(Enum):
    """Lifecycle of an in-progress agent-assisted merge resolution.

    PENDING   — merge started, conflict injected into the backend, waiting for
                the submit-Enter keystroke to land (``prompt_sent_at`` is None).
    RESOLVING — Enter was sent; waiting for the agent to finish the resolution
                turn (``prompt_sent_at`` is set; agent output is expected).
    MANUAL    — user chose manual resolution: agent NOT involved.  User edits
                files and then triggers "Complete merge" explicitly.
    """

    PENDING = "pending"
    RESOLVING = "resolving"
    MANUAL = "manual"


@dataclass
class MergeContext:
    """Replaces the ad-hoc ``merge_ctx`` dict.  Stored on ``Session.merge_ctx``.

    ``auto_tried`` is kept for backward compatibility with ProxyRunner.__new__
    test sites that check ``ctx.get("auto_tried")``.  New code should test
    ``phase`` instead.
    """

    source_branch: str
    context: str
    started: float = field(default_factory=time.monotonic)
    phase: MergePhase = MergePhase.PENDING
    # Backward-compat aliases used by existing runner code:
    # ``ctx["auto_tried"]`` → ``ctx.auto_tried``
    # ``ctx["prompt_sent_at"]`` → ``ctx.prompt_sent_at``
    auto_tried: bool = False
    prompt_sent_at: float | None = None

    # dict-style attribute access shim so existing ``ctx.get(key)`` /
    # ``ctx[key]`` call sites keep working without edits.
    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def __contains__(self, key):
        return hasattr(self, key)


# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------

INTEGRATED = "integrated"
CONFLICT   = "conflict"
SKIP       = "skip"


# ---------------------------------------------------------------------------
# IntegrationService
# ---------------------------------------------------------------------------

class IntegrationService:
    """Stateless (except for ``base_repo`` / ``base_branch``) integration logic.

    The runner creates one instance and keeps it alive; ``base_branch`` is
    updated in-place when the user switches the base (``_perform_base_switch``).

    All git-mutating methods are called with the *session's* ``GitRepo`` passed
    explicitly, never inferred from ``runner.active``, so they are safe inside
    temp-swap windows.
    """

    def __init__(self, base_repo: GitRepo, base_branch: str | None) -> None:
        self.base_repo = base_repo
        self.base_branch = base_branch  # mutable; updated by runner on base-switch

    # ------------------------------------------------------------------
    # Turn-branch lifecycle
    # ------------------------------------------------------------------

    def turn_from_branch(self, branch: str) -> int:
        """Parse the turn number from an agit turn-branch name."""
        match = re.search(r"/t(\d+)$", branch or "")
        return int(match.group(1)) if match else 0

    def ensure_turn_branch(
        self,
        repo: GitRepo,
        worktree,
        turn: int,
        worktree_manager: WorktreeManager,
        session_name: str,
        backend_name: str,
    ) -> int:
        """Create a new turn branch if the session is detached (between turns).

        Returns the new turn number (unchanged if a branch already existed).
        """
        if worktree is None or not repo.is_detached():
            return turn
        current = (turn or 0) + 1
        next_branch = worktree_manager.turn_branch(session_name, current, backend=backend_name)
        while repo.branch_exists(next_branch):
            current += 1
            next_branch = worktree_manager.turn_branch(session_name, current, backend=backend_name)
        repo.switch(next_branch, create=True)
        return current

    # ------------------------------------------------------------------
    # Base-alignment helpers
    # ------------------------------------------------------------------

    def align_session_to_base(self, repo: GitRepo) -> None:
        """Bring an idle session worktree up to date with the base branch.

        Two cases:
        - No unintegrated commits → re-point (detach) at the current base.
        - Has its own work → merge new base commits in cleanly; skip on conflict.
        A dirty or mid-merge worktree is left untouched.
        """
        if self.base_branch is None:
            return
        try:
            if repo.merge_in_progress() or repo.has_changes():
                return
            branch = repo.current_branch()
            if branch.startswith("agit/") and self.base_repo.log_range(self.base_branch, branch):
                if not self.base_repo.log_range(branch, self.base_branch):
                    return  # already contains the base
                if repo.merge(self.base_branch):
                    pass  # clean merge
                else:
                    repo.merge_abort()  # conflicts; leave for integration
                return
            if repo.rev_parse("HEAD") != self.base_repo.rev_parse(self.base_branch):
                repo.switch_detach(self.base_branch)
        except Exception:
            pass  # caller may log if needed

    def worktree_has_pending_work(self, repo: GitRepo, branch: str) -> bool:
        """Return True if the worktree has uncommitted changes or unintegrated commits."""
        try:
            if repo.has_changes():
                return True
            return bool(self.base_repo.log_range(self.base_branch, branch))
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Core integration
    # ------------------------------------------------------------------

    def integrate_turn_or_conflict(
        self,
        repo: GitRepo,
        name: str,
        worktree,
        merge_ctx,
        integration_paused: bool,
    ) -> tuple[str, str]:
        """Try to merge the base into the session's turn branch.

        Returns ``(outcome, turn_branch)`` where outcome is one of
        ``INTEGRATED``, ``CONFLICT``, or ``SKIP``.

        On ``INTEGRATED`` the base has been merged into the turn branch (clean
        merge) but the caller is responsible for fast-forwarding the base
        (``_advance_base_to`` / ``advance_base_to``) — this lets tests mock
        that step independently.
        Never touches UI.
        """
        if worktree is None or self.base_branch is None or merge_ctx:
            return SKIP, ""
        if integration_paused:
            return SKIP, ""
        turn_branch = repo.current_branch()
        if not turn_branch.startswith("agit/"):
            return SKIP, ""
        try:
            if not repo.merge(self.base_branch):
                repo.merge_abort()
                return CONFLICT, turn_branch
            return INTEGRATED, turn_branch
        except Exception:
            return SKIP, ""

    def advance_base_to(self, repo: GitRepo, source_branch: str) -> None:
        """Fast-forward the base to ``source_branch``, detach the session, delete the turn branch.

        Raises ``RuntimeError`` if the base repo is not on ``self.base_branch``
        (safety guard against advancing the wrong branch after an out-of-band
        ``git checkout``).
        """
        current = self.base_repo.current_branch()
        if current != self.base_branch:
            raise RuntimeError(
                f"base repo is on '{current}', not the integration branch '{self.base_branch}'"
            )
        self.base_repo.merge_ff_only(source_branch)
        repo.switch_detach(self.base_branch)
        if repo.current_branch() != source_branch:
            self.base_repo.delete_branch(source_branch, force=True)

    def _advance_base(self, repo: GitRepo, source_branch: str) -> None:
        """Internal helper that calls advance_base_to without raising to callers."""
        self.advance_base_to(repo, source_branch)

    def integrate_session_on_exit(self, repo: GitRepo, merge_ctx) -> None:
        """Clean up a session's branch on exit: integrate committed work or drop empty branch.

        Conflicts and dirty trees are left intact for the next startup.
        """
        if self.base_branch is None:
            return
        if merge_ctx or repo.merge_in_progress() or repo.has_changes():
            return
        branch = repo.current_branch()
        if not branch.startswith("agit/"):
            return
        try:
            if not self.base_repo.log_range(self.base_branch, branch):
                self._advance_base(repo, branch)
                return
            if repo.merge(self.base_branch):
                self._advance_base(repo, branch)
            else:
                repo.merge_abort()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Merge-context lifecycle
    # ------------------------------------------------------------------

    def make_merge_context(
        self,
        repo: GitRepo,
        source_branch: str,
        *,
        manual: bool = False,
    ) -> MergeContext:
        """Build a ``MergeContext`` for a newly-started merge resolution.

        Collects conflicting-commit context from the base repo; merges are
        called with the conflict already in progress in ``repo``.
        """
        files = repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self.base_branch, paths=files)
        except Exception:
            context = ""
        phase = MergePhase.MANUAL if manual else MergePhase.PENDING
        return MergeContext(
            source_branch=source_branch,
            context=context,
            phase=phase,
            auto_tried=manual,  # manual = auto was never tried / not desired
        )

    def merge_resolution_prompt(self, repo: GitRepo, source_branch: str) -> str:
        """Build the conflict-resolution prompt text to inject into the agent."""
        files = repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self.base_branch, paths=files)
        except Exception:
            context = ""
        listing = ", ".join(files) if files else "the conflicted files"
        commits = context.replace("\n", "; ") if context else "(none recorded)"
        return (
            f"[aGiT] Merge conflict: the base branch '{self.base_branch}' gained changes from another "
            f"session that conflict with your work in {listing}. The conflicting base commits are: {commits}. "
            "Please open the conflicted files, resolve every <<<<<<< / ======= / >>>>>>> marker keeping both "
            "changes' intent, and save. Do NOT run git or commit — aGiT will create the merge commit once you are done."
        )

    def should_auto_complete_merge(
        self,
        merge_ctx,
        last_child_output: float,
        child_idle_seconds: float,
    ) -> bool:
        """Return True if conditions are met to attempt auto-finalizing a merge.

        The auto-complete fires only after:
        1. The submit-Enter has been sent (``prompt_sent_at`` is set).
        2. The agent has produced output after that Enter.
        3. The agent has been idle for ``child_idle_seconds + 2`` seconds.
        4. Auto-complete has not already been attempted (``auto_tried`` is False).

        ``merge_ctx`` may be a :class:`MergeContext` or a legacy plain dict.
        """
        # Support both MergeContext (attribute access) and legacy plain dicts.
        get = merge_ctx.get if hasattr(merge_ctx, "get") else merge_ctx.__getitem__
        if get("auto_tried"):
            return False
        sent_at = get("prompt_sent_at") if "prompt_sent_at" in merge_ctx else None
        if not sent_at:
            return False
        if last_child_output <= sent_at:
            return False
        if time.monotonic() - last_child_output < child_idle_seconds + 2:
            return False
        return True

    def finalize_agent_merge(
        self,
        repo: GitRepo,
        merge_ctx: MergeContext,
        *,
        session_name: str,
        agit_session_id: str,
        backend_name: str,
        backend_session_id: str | None,
    ) -> tuple[bool, str | None]:
        """Attempt to finalize a pending agent merge.

        Returns ``(success, message)`` where ``message`` is a UI string to
        display (or None on conflict-markers present).  On success the caller
        must clear ``session.merge_ctx`` and clear ``agent_in_flight``.
        """
        # Support both MergeContext objects and legacy plain dicts.
        source_branch = (
            merge_ctx["source_branch"] if isinstance(merge_ctx, dict) else merge_ctx.source_branch
        )
        ctx_context = (
            merge_ctx.get("context") if isinstance(merge_ctx, dict) else merge_ctx.context
        )
        try:
            if not repo.merge_in_progress():
                return False, None  # already resolved/aborted elsewhere
            repo.add_all()
            if repo.has_conflict_markers() or repo.unmerged_paths():
                return False, (
                    "Conflict markers remain. Resolve them (or ask the agent again), "
                    "then Ctrl-G → session → Complete merge."
                )
            repo.commit(
                build_agent_merge_message(
                    session_name=session_name,
                    base_branch=self.base_branch,
                    source_branch=source_branch,
                    agit_session_id=agit_session_id,
                    backend=backend_name,
                    backend_session_id=backend_session_id,
                    conflicting_commits=ctx_context,
                )
            )
            self._advance_base(repo, source_branch)
            return True, f"Merge resolved and committed — integrated '{session_name}' into {self.base_branch}."
        except Exception:
            return False, None

    def start_merge(
        self,
        repo: GitRepo,
        name: str,
        worktree,
        *,
        auto: bool,
    ) -> tuple[str, MergeContext | None, str | None]:
        """Begin merging the base into the session's current turn branch.

        Returns ``(outcome, merge_ctx_or_none, message_or_none)``:
        - ``("clean", None, msg)`` — merged without conflicts; base advanced.
        - ``("conflict_auto", ctx, msg)`` — conflict; agent merge started (caller must inject prompt).
        - ``("conflict_manual", ctx, msg)`` — conflict; user resolves manually.
        - ``("error", None, msg)`` — merge could not start.
        - ``("skip", None, None)`` — no worktree or base branch.
        """
        if worktree is None or self.base_branch is None:
            return "skip", None, None
        source_branch = repo.current_branch()
        try:
            clean = repo.merge(self.base_branch)
        except Exception as error:
            return "error", None, f"Could not start merge: {error}"
        if clean:
            self._advance_base(repo, source_branch)
            return "clean", None, f"Integrated '{name}' into {self.base_branch} (no conflicts)."
        # Conflict: build context (files + log) once
        files = repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self.base_branch, paths=files)
        except Exception:
            context = ""
        ctx = MergeContext(
            source_branch=source_branch,
            context=context,
            phase=MergePhase.PENDING if auto else MergePhase.MANUAL,
            auto_tried=not auto,
        )
        if auto:
            return "conflict_auto", ctx, None
        msg = (
            f"Conflicts in {', '.join(files) or 'this session'}. Resolve them here (edit the files or ask the agent), "
            "then Ctrl-G → session → Complete merge."
        )
        return "conflict_manual", ctx, msg

    # ------------------------------------------------------------------
    # Cleanup / stale-worktree recovery
    # ------------------------------------------------------------------

    def cleanup_stale_worktree(
        self,
        info,
        worktree_manager: WorktreeManager,
    ) -> bool:
        """Integrate a dormant worktree's pending commits and delete it.

        Returns False (keep + flag) when it has uncommitted changes or its work
        conflicts with the base and needs manual resolution.
        """
        repo = GitRepo(info.path)
        if repo.merge_in_progress() or repo.has_changes():
            return False
        branch = repo.current_branch()
        if branch.startswith("agit/") and self.base_repo.log_range(self.base_branch, branch):
            if not repo.merge(self.base_branch):
                repo.merge_abort()
                return False
            self.base_repo.merge_ff_only(branch)
        worktree_manager.remove(info.name)
        return True

    def delete_orphan_merged_branches(self) -> None:
        """Remove agit/* branches that no worktree checks out and are in the base."""
        try:
            checked_out = {entry.get("branch") for entry in self.base_repo.worktree_list()}
            for branch in self.base_repo.list_branches("agit/"):
                if branch in checked_out:
                    continue
                if not self.base_repo.log_range(self.base_branch, branch):
                    self.base_repo.delete_branch(branch, force=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Session / base predicates
    # ------------------------------------------------------------------

    def session_unintegrated(self, repo: GitRepo | None) -> bool:
        """Return True if a session still has work that did not make it into the base."""
        try:
            if repo is None or repo.merge_in_progress() or repo.has_changes():
                return True
            branch = repo.current_branch()
            return branch.startswith("agit/") and bool(
                self.base_repo.log_range(self.base_branch, branch)
            )
        except Exception:
            return True

    def active_has_pending(self, repo: GitRepo, worktree) -> bool:
        """Return True if the active session has committed work not yet in the base."""
        if worktree is None or self.base_branch is None:
            return False
        try:
            return bool(self.base_repo.log_range(self.base_branch, repo.current_branch()))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Base-switch helpers
    # ------------------------------------------------------------------

    def base_switch_candidates(self) -> list[str]:
        """Return user branches the base could switch to (never agit/ branches)."""
        try:
            branches = self.base_repo.list_branches()
        except Exception:
            return []
        return [b for b in branches if not b.startswith("agit/") and b != self.base_branch]

    def repoint_to_base(self, repo: GitRepo, worktree) -> int | None:
        """Detach a session worktree at the new base; returns 0 (new turn counter) or None on skip."""
        if worktree is None:
            return None
        try:
            if repo.has_changes() or repo.merge_in_progress():
                return None
            repo.switch_detach(self.base_branch)
            return 0
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Base-drift / poll helpers
    # ------------------------------------------------------------------

    def check_base_drift(
        self,
        base_branch: str,
        integration_paused: bool,
        last_check_at: float,
        drift_check_seconds: float,
    ) -> tuple[bool, float, str | None]:
        """Check whether the base repo drifted off ``base_branch``.

        Returns ``(paused, new_check_at, message_or_none)``.  ``paused`` is the
        new value for ``_integration_paused``; ``message_or_none`` is a UI
        string to show (None = no change, no message needed).
        """
        now = time.monotonic()
        if now - last_check_at < drift_check_seconds:
            return integration_paused, last_check_at, None
        try:
            current = self.base_repo.current_branch()
        except Exception:
            return integration_paused, now, None
        drifted = current != base_branch
        if drifted and not integration_paused:
            msg = (
                f"⚠ Base branch changed outside aGiT — the repo is now on '{current}', but\n"
                f"aGiT integrates into '{base_branch}'. Worktree merging is PAUSED so\n"
                f"your work isn't merged into the wrong branch (sessions keep running and\n"
                f"committing to their own branches).\n"
                f"To resume: run  git checkout {base_branch}  in the repo — merging\n"
                f"continues automatically. To instead make '{current}' the base, quit and\n"
                f"relaunch aGiT from there."
            )
            return True, now, msg
        if not drifted and integration_paused:
            msg = f"Base branch back on '{base_branch}' — worktree merging resumed."
            return False, now, msg
        return integration_paused, now, None

    def poll_base_advanced(
        self,
        worktree,
        last_base_head: str | None,
        last_poll_at: float,
        base_poll_seconds: float,
    ) -> tuple[str | None, float, bool]:
        """Poll the base HEAD and detect out-of-band advances.

        Returns ``(new_head, new_poll_at, advanced)`` where ``advanced`` is True
        when the base moved since the last poll.
        """
        if worktree is None or self.base_branch is None:
            return last_base_head, last_poll_at, False
        now = time.monotonic()
        if now - last_poll_at < base_poll_seconds:
            return last_base_head, last_poll_at, False
        try:
            head = self.base_repo.rev_parse(self.base_branch)
        except Exception:
            return last_base_head, now, False
        advanced = last_base_head is not None and head != last_base_head
        return head, now, advanced
