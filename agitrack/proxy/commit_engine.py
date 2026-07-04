"""CommitEngine: the agent-commit pipeline extracted from ProxyRunner (#29, P4).

This module owns the stateless core of every agent-commit path:

* :meth:`CommitEngine.commit_turns` — trace-rebuild, token-accounting, commit-
  message assembly and ``git commit``.  Callers inject the two interactive
  behaviour differences as small callables so ONE implementation serves all
  three modes:

  - **proxy interactive** (ProxyRunner, popup UI):
      ``stage_untracked_fn = runner._review_untracked_popup``
      ``pre_commit_fn      = runner._ensure_turn_branch``
      ``on_commit_fn       = lambda sha: runner._set_message(...)``

  - **proxy exit / background** (non-interactive):
      ``stage_untracked_fn`` = auto-stage-non-declined closure
      (no ``pre_commit_fn``, no ``on_commit_fn``)

  - **actions / shell** (``AgitrackActions.create_agent_commit_from_turns``):
      ``stage_untracked_fn`` = ``actions.review_untracked(...)``
      (no ``pre_commit_fn``, no ``on_commit_fn``) — caller prints its own
      confirmation.

* :meth:`CommitEngine.finish_parse_if_ready` — awaited-followup logic and
  turn-completeness gate, extracted from
  ``ProxyRunner._finish_agent_parse_if_ready``.

* :meth:`CommitEngine.start_parse` — parse-worker launcher extracted from
  ``ProxyRunner._start_agent_parse``.  Results are always written back to the
  *owning* :class:`~agitrack.proxy.session.Session` (issue #15 fix, preserved
  verbatim).

* :meth:`CommitEngine.record_user_prompt` /
  :meth:`CommitEngine.await_followup` — thin state helpers.

* :meth:`CommitEngine.sanitize_state_trace` /
  :meth:`CommitEngine.initialize_session_baseline` /
  :meth:`CommitEngine.recover_nonempty_session` — session-startup helpers.

Design constraints
------------------
Every method that operates on git/state takes the *session bundle*
``(repo, state)`` (or a full :class:`~agitrack.proxy.session.Session`) explicitly
rather than touching ``self.active`` on the runner.  That makes it safe to
call from inside temp-swap windows (``_with_session``, ``_pump_background``,
``_stop_session``, ``_finalize_pending_work``) where ``active`` points at a
SERVICED session that may differ from the UI session.

What stays on ProxyRunner (not extracted here)
----------------------------------------------
* Debounce/status bookkeeping inside ``_maybe_agent_commit`` (uses
  runner-level ``file_change_event``, ``last_status``, ``last_poll``, …).
* ``_commit_latest_turn_sync`` and ``_finalize_pending_work`` (orchestrate the
  sync/exit flow over multiple sessions — runner scope).
* ``_commit_available_agent_turns`` (thin two-liner).
* ``_agent_commit_message`` (formats the UI popup text from
  ``_last_agent_commit_id`` stored on the runner).
* ``_ensure_turn_branch`` (worktree-branch management — requires
  ``self._worktrees()`` etc.).
* ``_mirror_session_to_base``, ``_stage_backend_resume``,
  ``_note_backend_session_change``, ``_should_continue_session``,
  ``_integrate_session_turn`` — runner-level coordination.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

from agitrack.commits import build_agent_commit_message, render_interaction_trace
from agitrack.git import GitRepo
from agitrack.transcripts.opencode import SessionTurn
from agitrack.transcripts import turns_after
from agitrack.config import AgitrackState


# ---------------------------------------------------------------------------
# Type aliases used in method signatures
# ---------------------------------------------------------------------------

_StageUntrackedFn = Callable[[GitRepo, AgitrackState], None]
"""Called to review/stage untracked files; receives (repo, state)."""

_PreCommitFn = Callable[[], None]
"""Called immediately before the ``git commit`` (e.g. ``_ensure_turn_branch``)."""

_OnCommitFn = Callable[[str | None, str, bool], None]
"""Called after a successful commit with: the short commit SHA (may be None), the
rendered interaction trace, and whether the commit is a *cover* (a merge-shaped
commit aGiTrack placed on top of the backend agent's own commits, #35)."""

_DebugFn = Callable[..., None]
"""Logging sink — ``runner._debug``."""


def _norm(text: str | None) -> str:
    """Whitespace-normalized form used to match recorded prompts against
    transcript turns (the transcript normalizes the user's raw typing)."""
    return " ".join((text or "").split())


def _is_slash_command(text: str | None) -> bool:
    """True when a typed prompt is purely a backend/TUI slash command
    (``/compact``, ``/comp``, ``/model``, ``/clear`` …).

    These are directives to the backend, not conversational prompts: the
    transcript parser already excludes them and :meth:`await_followup` skips
    them, so they must not be recorded into the pending trace either — otherwise
    they surface in the commit's interaction trace as a stray ``## User`` /
    ``/comp`` entry. A ``/compact`` in particular is redundant there: the trace's
    compaction lead-in note already records that the context was compacted."""
    return _norm(text).startswith("/")


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _same_prompt(a: str, b: str) -> bool:
    """True when two recordings are the same user prompt.

    The prompt recorded at submit reconstructs the raw typed bytes, so line
    editing while typing (cursor moves, deletions) garbles it relative to the
    transcript's clean version — same words, joined or reordered differently.
    Equality (even whitespace-normalized) misses those, leaving the garbled
    copy to be re-added to the trace as if it were a separate prompt. Near-
    duplicates are therefore detected by word overlap: editing artifacts only
    shuffle words around, while genuinely different prompts share few.
    """
    na, nb = _norm(a).lower(), _norm(b).lower()
    if na == nb:
        return True
    tokens_a, tokens_b = set(_TOKEN_RE.findall(na)), set(_TOKEN_RE.findall(nb))
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    return overlap >= 0.6


class CommitEngine:
    """Stateless agent-commit engine bound to a ``(repo, state)`` pair.

    The instance is cheap to construct and short-lived: the runner (or
    ``AgitrackActions``) creates one per commit call.  Nothing is cached on the
    instance between calls.
    """

    def __init__(
        self,
        repo: GitRepo,
        state: AgitrackState,
        *,
        debug_fn: _DebugFn | None = None,
        full_agent_messages: bool | None = None,
    ) -> None:
        self.repo = repo
        self.state = state
        self._debug = debug_fn or (lambda *a, **kw: None)
        # Per-run override for the "include all agent messages" behaviour (e.g. the
        # --full-agent-messages CLI flag). None defers to the per-repo config
        # (``state.full_agent_messages``); True/False forces it for this run only,
        # without touching the persisted config.
        self._full_agent_messages = full_agent_messages

    # ------------------------------------------------------------------
    # Core commit pipeline
    # ------------------------------------------------------------------

    def commit_turns(
        self,
        *,
        turns: list[SessionTurn],
        backend: str,
        backend_session_id: str | None,
        model: str | None,
        stage_untracked_fn: _StageUntrackedFn,
        pre_commit_fn: _PreCommitFn | None = None,
        on_commit_fn: _OnCommitFn | None = None,
        session_name: str | None = None,
        accumulate_trace_only_on_commit: bool = False,
        backend_commits: list[str] | None = None,
        manual_gate_fn: Callable[[], bool] | None = None,
        manual_record_fn: Callable[[str], str | None] | None = None,
    ) -> bool:
        """Core of every agent-commit path.

        Rebuilds the pending trace from *turns*, gates on ``has_staged_changes``,
        accumulates tokens, builds and records the commit.  Interactive
        differences (untracked review, branch preparation, message display) are
        fully injected so the same pipeline serves proxy-popup, proxy-quiet and
        actions/shell modes.

        Token usage is accumulated **only** once the commit actually happens
        (i.e. after ``has_staged_changes`` returns True).  A failed attempt
        (nothing staged) reprocesses the same turns on the next parse — the
        trace is rebuilt from scratch each call, but token usage is cumulative
        and would otherwise double-count.  (Bug #14, fixed in d041d10; both
        copies of the logic now share this single implementation.)

        Parameters
        ----------
        accumulate_trace_only_on_commit:
            When ``True`` (actions/shell mode), stage-check happens first and
            trace is only accumulated once the commit will definitely happen —
            leaving state pristine on a failed attempt so the caller can retry
            with fresh staging without a partial trace.  When ``False`` (proxy
            mode, the default), the trace is rebuilt before the stage check so
            the pending-user-merging logic can run even on failed attempts.
        backend_commits:
            Unintegrated commits the backend made itself this turn (full SHAs,
            oldest first), issue #35.  With nothing staged, a merge-shaped
            *cover commit* carrying the trace/metadata is added on top of them
            (their hashes never change — an amend would break references the
            agent already published, #58); with staged changes, the normal
            commit lists them in its ``covered_commits`` metadata.

        Commits are created immediately, without any LLM call: summarization
        runs in the background afterwards and is attached by amending the
        commit message (issue #8) — blocking the commit on a summary froze the
        UI and delayed integration past the next turn.

        Returns ``True`` if a commit was made, ``False`` otherwise.
        """
        if not turns:
            return False
        backend_commits = list(backend_commits or [])

        if accumulate_trace_only_on_commit:
            # Actions / shell mode: do the staged check first, accumulate only
            # when we know the commit will happen.  Leaves state pristine on
            # a failed attempt (nothing staged → no trace, no tokens written).
            # The pre-commit hook runs first either way (same ordering as the
            # proxy branch) so the parameter contract is mode-independent.
            if pre_commit_fn is not None:
                pre_commit_fn()
            self.repo.add_tracked()
            stage_untracked_fn(self.repo, self.state)
            cover_backend_head = False
            cover_with_staged = False
            if not self.repo.has_staged_changes():
                if not self._head_is_coverable(backend_commits):
                    return False
                cover_backend_head = True
            elif self._head_is_coverable(backend_commits):
                # Staged changes on top of coverable backend commits: cover them
                # together so the covered changes aren't hidden behind a plain
                # commit's single parent (#35).
                cover_with_staged = True
            # Commit (or cover) will happen: accumulate trace and tokens now.
            for turn in turns:
                if turn.user_prompt:
                    self.state.append_trace("user", turn.user_prompt)
                for message in self._agent_messages_for(turn):
                    self.state.append_trace("agent", message)
                self.state.add_token_usage(turn.tokens)
            prompts = [t.user_prompt for t in turns if t.user_prompt]
            subject_text = " / ".join(prompts) if prompts else f"{backend} changes"
        else:
            # Proxy mode: rebuild trace from scratch, preserving any pending user
            # entries that hadn't yet landed as a turn (e.g. a queued prompt from
            # before this parse cycle).
            pending_users: list[str] = [
                content
                for item in self.state.pending_trace()
                if item.get("role") == "user" and (content := item.get("content"))
            ]
            self.state.data["pending_trace"] = []
            self.state.save()

            subject_prompts: list[str] = []
            entries: list[tuple[str, str]] = []
            for turn in turns:
                if turn.user_prompt:
                    subject_prompts.append(turn.user_prompt)
                    entries.append(("user", turn.user_prompt))
                for message in self._agent_messages_for(turn):
                    entries.append(("agent", message))

            # Pending user entries that never showed up as a turn's user_prompt
            # (e.g. a follow-up note typed mid-turn) are still added to the
            # subject and trace so they appear in the commit body. Matching uses
            # word overlap (_same_prompt), not equality: the recording keeps the
            # user's raw typing, which line editing garbles relative to the
            # transcript's clean version — equality re-added the same prompt as
            # if it were new (issue #8). Duplicate recordings also collapse.
            turn_prompts = [t.user_prompt for t in turns if t.user_prompt]
            leftovers: list[str] = []
            for pending_user in pending_users:
                if not _norm(pending_user):
                    continue
                if _is_slash_command(pending_user):
                    continue  # a backend directive (e.g. /compact) recorded earlier — not a prompt
                if any(_same_prompt(pending_user, prompt) for prompt in turn_prompts):
                    continue
                if any(_same_prompt(pending_user, prompt) for prompt in leftovers):
                    continue
                leftovers.append(pending_user)
            subject_prompts.extend(leftovers)

            # Leftovers were typed while the LAST turn was running, so they belong with
            # that turn's prompt — right after it and BEFORE the turn's agent response(s).
            # A turn is laid out as [user_prompt, agent_msg, agent_msg, …], so insert the
            # leftovers immediately after the last user prompt. The previous logic inserted
            # before the last AGENT message, which — when a turn emitted several agent
            # messages — dropped the leftovers in between (or after) the agent's replies,
            # so a message the user sent mid-turn read as if it came after the agent's
            # final response (issue #8; the agent's answer actually covers all of them).
            insert_at = len(entries)
            for index in range(len(entries) - 1, -1, -1):
                if entries[index][0] == "user":
                    insert_at = index + 1
                    break
            entries[insert_at:insert_at] = [("user", leftover) for leftover in leftovers]
            for role, content in entries:
                self.state.append_trace(role, content)

            cover_backend_head = False
            cover_with_staged = False
            if manual_record_fn is not None:
                # Manual-commit mode: never stage into the user's index, run pre_commit_fn
                # (no turn branch), or move HEAD. The turn is recorded as a hidden latent
                # commit by manual_record_fn instead. Gate on whether the working tree
                # actually changed since the latent tip, so a no-op turn records nothing —
                # and, per bug #14, tokens are still accumulated only once past this gate.
                if manual_gate_fn is not None and not manual_gate_fn():
                    return False
            else:
                # Hook: proxy mode puts the session on a fresh turn branch here.
                if pre_commit_fn is not None:
                    pre_commit_fn()

                self.repo.add_tracked()
                stage_untracked_fn(self.repo, self.state)

                if not self.repo.has_staged_changes():
                    if not self._head_is_coverable(backend_commits):
                        return False
                    cover_backend_head = True
                elif self._head_is_coverable(backend_commits):
                    # Staged changes on top of coverable backend commits: cover them
                    # together so the covered changes aren't hidden behind a plain
                    # commit's single parent (#35).
                    cover_with_staged = True

            # Accumulate tokens only once we know the commit (or cover) will happen.
            for turn in turns:
                self.state.add_token_usage(turn.tokens)

            subject_text = " / ".join(subject_prompts) if subject_prompts else f"{backend} changes"

        # The metadata lists covered hashes in short form (the full SHAs stay
        # internal — _head_is_coverable compares them against rev-parse HEAD).
        # An aGiTrack commit accounts for itself; it lists only the backend-made
        # commits it additionally covers (#35).
        covered_display = [self._short_sha(sha) for sha in backend_commits]
        # The AI-driven conversation's span: earliest prompt to latest response
        # across the turns this commit accounts for (None when the backend's
        # transcript carries no timestamps).
        starts = [turn.started_at for turn in turns if turn.started_at]
        ends = [turn.ended_at for turn in turns if turn.ended_at]
        # Context compactions across the turns this commit accounts for, and a one-shot
        # fork/copy origin event for this session — both recorded because they reshape
        # the context the token counts run against (issue: track compaction & fork/copy).
        compactions = sum(int(getattr(turn, "compaction_count", 0) or 0) for turn in turns)
        # Reasoning effort / thinking level for this commit's turns: the most recent
        # turn that recorded one wins, so the metadata reflects the level in effect
        # at the end of the span (None when no turn revealed it).
        reasoning_effort = next(
            (turn.reasoning_effort for turn in reversed(turns) if getattr(turn, "reasoning_effort", None)),
            None,
        )
        # Anchor into the backend conversation: the message id of the last turn this
        # commit covers, so the commit links back to the exact place in the (locally
        # kept or shared) transcript. The last turn with a recorded assistant message
        # id wins; None when the backend exposes none.
        conversation_anchor = next(
            (turn.assistant_message_id for turn in reversed(turns) if getattr(turn, "assistant_message_id", None)),
            None,
        )
        origin_event = self.state.session_origin_event()
        message = build_agent_commit_message(
            latest_prompt=subject_text,
            trace=self.state.pending_trace(),
            backend=backend,
            backend_session_id=backend_session_id,
            agitrack_session_id=self.state.session_id,
            model=model or self.state.model,
            reasoning_effort=reasoning_effort,
            conversation_anchor=conversation_anchor,
            token_usage=self.state.pending_token_usage(),
            trace_turn_limit=self.state.trace_turn_limit,
            session_name=session_name,
            covered_commits=covered_display or None,
            started_at=min(starts) if starts else None,
            ended_at=max(ends) if ends else None,
            compactions=compactions,
            origin_event=origin_event,
        )
        if manual_record_fn is not None:
            # Manual-commit mode: record the turn as a hidden latent commit on the side
            # ref (snapshot the working tree, commit-tree onto the latent tip, move only
            # that ref). HEAD and the user's index are untouched — the user's own commit
            # later folds these in. Returns the latent sha (or None if, defensively, the
            # tree turned out unchanged after the gate).
            commit_sha = manual_record_fn(message)
            if commit_sha is None:
                return False
        elif cover_backend_head or cover_with_staged:
            # The backend committed its own work (#35). Its commits keep their
            # hashes — amending them broke references the agent had already
            # published in PRs/issues (#58). Instead the trace/metadata ride a
            # GitHub-PR-style merge-shaped cover commit on top, parents (turn
            # start, backend head), so `git log --first-parent` reads turn-by-turn
            # and the cover's diff shows every covered change. When aGiTrack also has
            # extra staged changes on top (e.g. it staged the agent's new files),
            # `include_staged` folds them into the cover's tree so they're tracked
            # alongside the covered commits rather than hidden behind a plain
            # commit's single parent.
            commit_sha = self.repo.cover_commit(
                message,
                first_parent=f"{backend_commits[0]}^",
                second_parent=backend_commits[-1],
                include_staged=cover_with_staged,
            )
        else:
            commit_sha = self.repo.commit(message)
        # The fork/copy origin event is a one-shot: it has now been surfaced in this
        # commit, so clear it (a no-op when there was none) — later commits in the same
        # session shouldn't keep re-announcing the lineage.
        if origin_event is not None:
            self.state.clear_session_origin_event()
        # Render the interaction trace exactly as it landed in the commit, BEFORE
        # clearing it, and hand it to on_commit_fn — this is the summarizer's sole
        # input. (Capturing it in the caller before commit_turns was wrong: the
        # proxy branch above clears pending_trace and rebuilds it from the turns,
        # so a pre-commit capture saw only stray leftover prompts, which made the
        # summary empty/garbage and often unusable.)
        trace_text = render_interaction_trace(self.state.pending_trace(), self.state.trace_turn_limit)
        self.state.clear_trace()

        if on_commit_fn is not None:
            on_commit_fn(commit_sha, trace_text, cover_backend_head or cover_with_staged)

        return True

    def _agent_messages_for(self, turn: SessionTurn) -> list[str]:
        """The agent's user-facing message(s) to record in the trace for *turn*.

        By default just the final response — the substantive reply, and the long-
        standing behaviour. When the ``full_agent_messages`` option is on, every
        user-facing message the agent sent during the turn, in order (each becomes
        its own ``## Agent`` block); tool calls, tool results, and file edits are
        never included either way. Falls back to the final response when the backend
        didn't recover the full list, and is empty when the turn has no agent text.
        """
        full = self._full_agent_messages
        if full is None:
            full = self.state.full_agent_messages
        if full and turn.agent_messages:
            return [message for message in turn.agent_messages if message]
        return [turn.final_response] if turn.final_response else []

    def _short_sha(self, sha: str) -> str:
        """Short display form of *sha* (falls back to a 7-char prefix when the
        repo cannot resolve it, e.g. fake repos in tests)."""
        try:
            return self.repo.short_sha(sha)
        except Exception:
            return sha[:7]

    def _head_is_coverable(self, backend_commits: list[str]) -> bool:
        """True when HEAD is the latest of the backend's own unintegrated
        commits, so a cover commit on top attaches the trace to the commits
        that actually made the change (#35). Never true for commits aGiTrack
        created (they carry their own metadata) or for anything already
        integrated into base (``backend_commits`` only ever lists commits
        ahead of base)."""
        if not backend_commits:
            return False
        try:
            return self.repo.rev_parse("HEAD") == backend_commits[-1]
        except Exception as error:
            self._debug(f"cover check failed: {error!r}")
            return False

    # ------------------------------------------------------------------
    # Parse-result consumption (extracted from _finish_agent_parse_if_ready)
    # ------------------------------------------------------------------

    def finish_parse_if_ready(
        self,
        *,
        session,  # agitrack.proxy.session.Session
        quiet: bool,
        prompt_untracked: bool,
        require_complete: bool,
        awaited_followups: list[str],
        agent_is_active_fn: Callable[[], bool],
        debug_fn: _DebugFn,
        note_session_change_fn: Callable[[str], None],
        mirror_fn: Callable[[str | None], None],
        commit_fn: Callable,
        on_cancelled_fn: Callable[[list[SessionTurn]], bool] | None = None,
    ) -> tuple[bool | None, list[str]]:
        """Consume a ready parse result and (conditionally) commit.

        Extracted from ``ProxyRunner._finish_agent_parse_if_ready``.  The
        caller retains ownership of the ``awaited_followups`` list; the
        updated list is returned as the second element of the tuple.

        ``commit_fn`` is called with ``(turns=..., backend=...,
        backend_session_id=..., model=..., quiet=..., prompt_untracked=...)``
        keyword arguments when a commit should happen.  The runner passes
        ``self._create_agent_commit_from_turns_popup`` so test mocks stay
        effective; ``AgitrackActions`` passes its own pipeline wrapper.

        Returns
        -------
        (result, new_awaited)
            *result* is ``True`` (committed), ``False`` (consumed, no commit),
            or ``None`` (deferred / no result ready).
        """
        parse_thread = session.agent_parse_thread
        if parse_thread and parse_thread.is_alive():
            return None, awaited_followups

        parse_result = session.agent_parse_result
        if parse_result is None:
            return None, awaited_followups

        session.agent_parse_result = None
        session_id, exported_session, last_message_id, owner_state = parse_result

        if owner_state is not None and owner_state is not self.state:
            debug_fn("discarding agent parse result owned by another session")
            return None, awaited_followups

        if not exported_session:
            debug_fn(f"agent parse consumed without session session_id={session_id}")
            return False, awaited_followups

        new_session_id = exported_session.session_id or session_id
        note_session_change_fn(new_session_id)
        self.state.backend_session_id = new_session_id
        mirror_fn(new_session_id)

        if exported_session.model:
            self.state.model = exported_session.model

        all_turns = turns_after(exported_session, last_message_id)

        # Awaited-followup logic: a prompt queued while the agent was busy
        # belongs in the same commit as the turn it triggered.
        awaited = list(awaited_followups)
        if awaited and any(getattr(t, "interrupted", False) for t in all_turns):
            awaited = []
        if awaited:
            # Word-overlap matching, not equality: line editing garbles the
            # recorded prompt relative to the transcript's clean version, and
            # an unmatchable awaited entry would defer commits indefinitely.
            turn_prompts = [t.user_prompt or "" for t in exported_session.turns]
            awaited = [p for p in awaited if not any(_same_prompt(p, prompt) for prompt in turn_prompts)]
            if require_complete and awaited and agent_is_active_fn():
                debug_fn(f"deferring agent commit: {len(awaited)} queued follow-up(s) not yet in transcript")
                return None, awaited
            awaited = []  # committing now — drop cancelled queue entries

        if require_complete and all_turns and not all_turns[-1].complete:
            debug_fn(f"deferring agent commit: latest turn still in progress session_id={new_session_id}")
            return None, awaited

        complete_turns = [t for t in all_turns if t.final_response]
        if not complete_turns:
            # A user-cancelled turn (Esc) that produced no committable response
            # would normally leave the agent's partial edits sitting uncommitted in
            # the worktree forever. When a cancellation handler is supplied (the
            # live interactive path), let it decide commit-vs-discard; if it acted,
            # advance the watermark past the cancelled turn so it isn't reconsidered.
            last_turn = all_turns[-1] if all_turns else None
            if (
                on_cancelled_fn is not None
                and last_turn is not None
                and getattr(last_turn, "interrupted", False)
                and on_cancelled_fn(all_turns)
            ):
                self.state.set_backend_message_id(
                    self.state.backend_session_id,
                    last_turn.assistant_message_id
                    or last_turn.user_message_id
                    or self.state.backend_message_id_for(self.state.backend_session_id),
                )
            debug_fn(
                f"agent parse consumed without final response "
                f"session_id={self.state.backend_session_id} turns={len(all_turns)}"
            )
            return False, awaited

        committed = commit_fn(
            turns=all_turns,
            backend=session.backend.name,
            backend_session_id=self.state.backend_session_id,
            model=exported_session.model or self.state.model,
            quiet=quiet,
            prompt_untracked=prompt_untracked,
        )
        if committed:
            # Advance the watermark for THIS conversation so the next parse cycle only
            # exports its new turns — keyed per conversation so a later switch back to a
            # different conversation reads its own mark, never this one's.
            self.state.set_backend_message_id(self.state.backend_session_id, complete_turns[-1].assistant_message_id)
            debug_fn(
                f"agent commit created session_id={self.state.backend_session_id} "
                f"assistant_id={self.state.last_backend_message_id}"
            )
        return committed, awaited

    # ------------------------------------------------------------------
    # Parse-worker launcher (extracted from _start_agent_parse)
    # ------------------------------------------------------------------

    def start_parse(
        self,
        *,
        session,  # agitrack.proxy.session.Session
        discover_session_id_fn: Callable[[], str | None],
        debug_fn: _DebugFn,
    ) -> bool:
        """Launch the background session-export worker for *session*.

        Verbatim semantics from ``ProxyRunner._start_agent_parse``:

        * Results are written back to the *owning* Session object (issue #15).
        * ``agent_parse_lock`` prevents double-launch.
        * Returns ``False`` if a worker is already running or a result is
          already pending.

        Parameters
        ----------
        session:
            The session whose backend will be exported.  All reads and writes
            happen on this object; the caller may swap ``active`` freely while
            the worker runs.
        discover_session_id_fn:
            Called only when ``session.worktree is None`` to discover the
            spawned backend session (``runner._discover_spawned_session``).
        debug_fn:
            Logging sink.
        """
        parse_lock = session.agent_parse_lock
        if parse_lock is None:
            parse_lock = threading.Lock()
            session.agent_parse_lock = parse_lock

        with parse_lock:
            if session.agent_parse_active:
                return False
            if session.agent_parse_thread and session.agent_parse_thread.is_alive():
                return False
            if session.agent_parse_result is not None:
                return False
            session.agent_parse_active = True

        owner = session
        backend = owner.backend
        repo = owner.repo
        state = owner.state
        worktree = owner.worktree

        def worker() -> None:
            result = None
            try:
                debug_fn("agent parse worker started")
                if worktree is not None:
                    # A worktree's directory is unique to this aGiTrack session, so
                    # the newest backend session there is always this session's
                    # current conversation — track it even if the user started a
                    # new conversation from inside the backend.
                    session_id = backend.latest_session_id(repo.repo) or state.backend_session_id
                else:
                    # No worktree isolation: track the session aGiTrack spawned, and follow an
                    # in-backend switch (Claude /resume or a new conversation started inside the
                    # backend) too, so ALL modes support session switching. discover_session_id_fn
                    # is snapshot-based: it only ever returns a session that appeared AFTER launch,
                    # never a pre-existing unrelated one, so preferring it can't grab the wrong
                    # session — and it returns None (⇒ fall back to the pinned id) when no switch
                    # has happened or no reliable snapshot exists. The per-conversation watermark
                    # (backend_message_id_for) keeps each conversation counted exactly once.
                    session_id = discover_session_id_fn() or state.backend_session_id
                # Read the watermark for the conversation actually being exported (not a
                # single global one), so switching between conversations never replays or
                # double-counts a conversation's already-committed turns.
                last_message_id = state.backend_message_id_for(session_id)
                exported = backend.export_session(repo.repo, session_id) if session_id else None
                turn_count = len(exported.turns) if exported else 0
                final_count = len([t for t in exported.turns if t.final_response]) if exported else 0
                debug_fn(
                    f"agent parse worker finished session_id={session_id} turns={turn_count} "
                    f"finals={final_count} watermark={last_message_id}"
                )
                result = (session_id, exported, last_message_id, state)
            finally:
                with parse_lock:
                    owner.last_parse_finish = time.monotonic()
                    if result is not None:
                        owner.agent_parse_result = result
                    owner.agent_parse_active = False

        session.last_parse_start = time.monotonic()
        debug_fn("agent parse started")
        session.agent_parse_thread = threading.Thread(target=worker, name="agit-session-parse", daemon=True)
        session.agent_parse_thread.start()
        return True

    # ------------------------------------------------------------------
    # Simple state helpers
    # ------------------------------------------------------------------

    def record_user_prompt(self, prompt_text: str) -> None:
        """Append a user prompt to the pending trace (no-op if empty or a bare
        slash command). Slash commands (``/compact``, ``/model``, …) are backend
        directives, not prompts, and are kept out of the trace — see
        :func:`_is_slash_command`."""
        if prompt_text and not _is_slash_command(prompt_text):
            self.state.append_trace("user", prompt_text)

    def await_followup(self, prompt_text: str, awaited: list[str]) -> list[str]:
        """Return a new awaited list with *prompt_text* appended if appropriate.

        Slash commands (/model, /compact, …) are skipped because they are
        filtered from the transcript and would defer commits indefinitely.
        The updated list must be stored by the caller (on the runner or
        wherever ``_awaited_followups`` lives).
        """
        norm = _norm(prompt_text)
        if norm and not _is_slash_command(prompt_text):
            return awaited + [norm]
        return awaited

    # ------------------------------------------------------------------
    # Session-baseline helpers (extracted from runner startup paths)
    # ------------------------------------------------------------------

    def sanitize_state_trace(self, backend) -> None:
        """Drop raw backend event blobs from the pending trace.

        Certain backends (e.g. OpenCode) persist large JSON event objects in
        the trace.  They are not human-readable and bloat commit messages.
        This strips them in-place and saves state.
        """
        changed = False
        clean = []
        for item in self.state.pending_trace():
            role = item.get("role")
            content = item.get("content")
            if role == "agent" and isinstance(content, str) and backend.is_event_blob(content):
                changed = True
                continue
            clean.append(item)
        if changed:
            self.state.data["pending_trace"] = clean
            self.state.save()
            self._debug("removed raw backend event blob from pending trace")

    def recover_nonempty_session(self, backend, repo, stage_backend_resume_fn):
        """Find the most recent non-empty conversation for this worktree.

        Called when the recorded session turns out empty.  Returns
        ``(session_id, ExportedSession)`` or ``None``.
        """
        try:
            candidate = backend.latest_session_id(repo.repo)
        except Exception as error:
            self._debug(f"recover non-empty session failed: {error!r}")
            return None
        if not candidate or candidate == self.state.backend_session_id:
            return None
        stage_backend_resume_fn(candidate)
        session = backend.export_session(repo.repo, candidate)
        if session and session.turns:
            return candidate, session
        return None

    def initialize_session_baseline(
        self,
        backend,
        repo,
        *,
        should_continue_fn: Callable[[], bool],
        stage_backend_resume_fn: Callable[[str | None], None],
        debug_fn: _DebugFn | None = None,
    ) -> None:
        """Compute the resume baseline for a newly-spawned session.

        Mirrors ``ProxyRunner._initialize_session_baseline``.  The caller's
        ``stage_backend_resume_fn`` is invoked to copy the transcript into the
        right directory before export.
        """
        _dbg = debug_fn or self._debug
        if not should_continue_fn():
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            return
        stage_backend_resume_fn(self.state.backend_session_id)
        session = backend.export_session(repo.repo, self.state.backend_session_id)
        if not session or not session.turns:
            recovered = self.recover_nonempty_session(backend, repo, stage_backend_resume_fn)
            session = recovered[1] if recovered else None
            if recovered:
                _dbg(f"recorded session empty; recovered non-empty {recovered[0]}")
                self.state.backend_session_id = recovered[0]
            else:
                self.state.backend_session_id = None
                self.state.last_backend_message_id = None
                return
        if session.model:
            self.state.model = session.model
        complete = [t for t in session.turns if t.assistant_message_id]
        self.state.last_backend_message_id = complete[-1].assistant_message_id if complete else None
        self.state.clear_trace()
