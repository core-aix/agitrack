"""Pipeline-level user-flow tests: real git + a fake backend, no agent CLI.

The rest of the commit-engine suite mocks ``repo.commit`` (it only records the message), so a
REAL git failure — e.g. the Windows cp1252 commit-message encoding bug — slips straight through.
These tests instead drive the REAL :class:`CommitEngine` against a REAL temporary git repository,
with backend behaviour faked via scripted transcript data (:class:`FakeBackend`), so a flow is
exercised end-to-end without launching ``claude``/``opencode``. They run on every CI OS, including
the Windows job where that encoding bug actually reproduced.

This is the foundation of the pipeline-level flow harness: extend it by scripting more turns /
user edits and asserting on the resulting real git state.
"""

from __future__ import annotations

import types
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.transcripts import ExportedSession, SessionRef, SessionTurn


class FakeBackend:
    """A backend stand-in that returns scripted transcript data — never launches a CLI.

    Only the surface the pipeline actually reads is implemented; extend as flows are added."""

    name = "claude"
    supports_session_sharing = False

    def __init__(self, turns: list[SessionTurn] | None = None, session_id: str = "s1") -> None:
        self._turns = turns or []
        self._session_id = session_id

    def export_session(self, repo: Path, session_id: str) -> ExportedSession:
        return ExportedSession(session_id, None, None, list(self._turns))

    def list_sessions(self, repo: Path) -> list[SessionRef]:
        return [SessionRef(id=self._session_id, updated=0.0, label=None)]

    def latest_session_id(self, repo: Path) -> str | None:
        return self._session_id

    def new_session_id(self) -> str | None:
        return None


def _turn(prompt: str, response: str, *, assistant_id: str = "aid") -> SessionTurn:
    return SessionTurn(
        "uid",
        assistant_id,
        prompt,
        response,
        TokenUsage(total=1, output=1, input=0),
        None,
        complete=True,
    )


def _repo_with_state(tmp_path: Path) -> tuple[GitRepo, AgitrackState]:
    repo = GitRepo.init(tmp_path)
    return repo, AgitrackState(tmp_path)


def _noop_stage(repo, state) -> None:
    pass


def test_agent_turn_commit_lands_in_real_git_with_unicode_trace(tmp_path: Path):
    # The flow that broke on Windows: an agent turn whose trace carries box-drawing/em-dash/emoji
    # must produce a REAL commit. The old mock-based engine tests stored the message and so never
    # caught the cp1252 encoding failure; this drives real `git commit`.
    repo, state = _repo_with_state(tmp_path)
    (tmp_path / "game.py").write_text("print('hi')\n", encoding="utf-8")
    repo.stage_paths(["game.py"])

    committed = CommitEngine(repo, state).commit_turns(
        turns=[_turn("add a game —", "Done. Menu drawn:\n┌─ play\n└─ quit  \U0001f3ae")],
        backend="claude",
        backend_session_id="s1",
        model="claude-opus-4-8",
        stage_untracked_fn=_noop_stage,
    )

    assert committed is True
    body = repo._run(["git", "log", "-1", "--pretty=%B"]).stdout
    assert "┌─ play" in body  # the box-drawing trace survived into a real commit
    assert "\U0001f3ae" in body
    # A real commit object exists (working tree clean afterward).
    assert repo._run(["git", "status", "--porcelain"]).stdout.strip() == ""


def test_agent_commit_is_skipped_when_nothing_is_staged(tmp_path: Path):
    # No staged changes → no commit (the engine must not create an empty commit or raise).
    repo, state = _repo_with_state(tmp_path)
    head_before = repo.rev_parse("HEAD")

    committed = CommitEngine(repo, state).commit_turns(
        turns=[_turn("noop", "nothing to change")],
        backend="claude",
        backend_session_id="s1",
        model="claude-opus-4-8",
        stage_untracked_fn=_noop_stage,
    )

    assert committed is False
    assert repo.rev_parse("HEAD") == head_before  # no new commit


# --- Challenging interaction scenarios: interruption + follow-up prompts before a commit ------
#
# These drive the REAL parse->commit decision (CommitEngine.finish_parse_if_ready), which is
# where the tricky timing lives: a user pressing Esc mid-turn, and a follow-up prompt sent while
# the agent is still working on the previous one. The point is that aGiTrack must never (a) lose
# the agent's partial edits on an interrupt, (b) commit before a queued follow-up has landed, or
# (c) split one prompt across commits.


def _turn_full(
    prompt: str,
    response: str,
    *,
    uid: str = "u",
    aid: str = "a",
    complete: bool = True,
    interrupted: bool = False,
) -> SessionTurn:
    return SessionTurn(
        uid,
        aid,
        prompt,
        response,
        TokenUsage(total=1, output=1 if response else 0, input=0 if response else 1),
        None,
        complete=complete,
        interrupted=interrupted,
    )


def _session_for(state, turns: list[SessionTurn], *, backend: str = "claude", last_message_id=None):
    """A minimal Session-shaped object carrying a ready parse result for finish_parse_if_ready."""
    parse_result = ("s1", ExportedSession("s1", None, None, turns), last_message_id, state)
    return types.SimpleNamespace(
        agent_parse_thread=None,
        agent_parse_result=parse_result,
        backend=types.SimpleNamespace(name=backend),
    )


def _finish(engine, session, **overrides):
    kwargs = dict(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda _m: None,
        note_session_change_fn=lambda _sid: None,
        mirror_fn=lambda _sid: None,
        commit_fn=lambda **_k: True,
        on_cancelled_fn=None,
    )
    kwargs.update(overrides)
    return engine.finish_parse_if_ready(**kwargs)


def test_interrupted_turn_routes_to_cancellation_handler_not_a_commit(tmp_path: Path):
    # User pressed Esc: the turn is interrupted with no final response, but the agent may have left
    # partial edits. It must go to the cancellation handler (which decides commit-vs-discard so the
    # edits aren't stranded) — NOT the normal agent-commit path.
    repo, state = _repo_with_state(tmp_path)
    engine = CommitEngine(repo, state)
    session = _session_for(state, [_turn_full("big refactor", "", complete=True, interrupted=True)])
    seen: list[str] = []
    result, _awaited = _finish(
        engine,
        session,
        commit_fn=lambda **_k: seen.append("commit") or True,
        on_cancelled_fn=lambda _turns: (seen.append("cancelled"), True)[1],
    )
    assert seen == ["cancelled"]  # routed to the cancellation handler, never committed
    assert result is False


def test_followup_queued_before_its_turn_lands_defers_the_commit(tmp_path: Path):
    # A follow-up ("add tests") was sent while the agent was still on the first prompt, so it isn't
    # in the transcript yet. The commit must be DEFERRED (not made for the first turn alone) until
    # the follow-up's turn appears — otherwise the follow-up is split into a separate commit.
    repo, state = _repo_with_state(tmp_path)
    engine = CommitEngine(repo, state)
    session = _session_for(state, [_turn_full("add a feature", "Done.")])
    result, awaited = _finish(
        engine,
        session,
        awaited_followups=["add tests"],
        agent_is_active_fn=lambda: True,  # still working on the queued follow-up
    )
    assert result is None  # deferred
    assert "add tests" in awaited  # still waiting for it to land


def test_followup_that_landed_is_committed_with_its_turn(tmp_path: Path):
    # Once the follow-up's turn is in the transcript, BOTH prompts commit together (one commit).
    repo, state = _repo_with_state(tmp_path)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    engine = CommitEngine(repo, state)
    session = _session_for(
        state,
        [
            _turn_full("add a feature", "Done.", uid="u1", aid="a1"),
            _turn_full("add tests", "Tests added.", uid="u2", aid="a2"),
        ],
    )

    def real_commit(**k):
        return engine.commit_turns(
            turns=k["turns"],
            backend=k["backend"],
            backend_session_id=k["backend_session_id"],
            model=k["model"],
            stage_untracked_fn=_noop_stage,
        )

    result, awaited = _finish(engine, session, awaited_followups=["add tests"], commit_fn=real_commit)
    assert result is True and awaited == []
    body = repo._run(["git", "log", "-1", "--pretty=%B"]).stdout
    assert "add a feature" in body and "add tests" in body  # both prompts in the single commit


def test_incomplete_latest_turn_defers_until_it_finishes(tmp_path: Path):
    # The agent's last message was a tool call, not a final answer (complete=False): don't commit
    # a half-finished turn — wait for it to complete.
    repo, state = _repo_with_state(tmp_path)
    engine = CommitEngine(repo, state)
    session = _session_for(state, [_turn_full("working on it", "", complete=False)])
    committed: list = []
    result, _awaited = _finish(
        engine,
        session,
        agent_is_active_fn=lambda: True,
        commit_fn=lambda **_k: committed.append(1) or True,
    )
    assert result is None and committed == []  # deferred, nothing committed


def test_commit_raises_catchable_giterror_on_failing_pre_commit_hook(tmp_path: Path):
    # The real-git counterpart of test_user_commit_popup_surfaces_failure_without_crashing: a repo
    # pre-commit hook that rejects the commit must surface as a catchable GitError (which the
    # user-commit popup then reports instead of crashing), and the staged changes must be PRESERVED
    # so the user can fix the cause and retry — never silently lost.
    import pytest

    from agitrack.git.repo import GitError

    repo = GitRepo.init(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook rejected' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (tmp_path / "f.txt").write_text("change\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])

    with pytest.raises(GitError):
        repo.commit("a message")

    staged = repo._run(["git", "diff", "--cached", "--name-only"]).stdout
    assert "f.txt" in staged  # changes left staged, not lost
