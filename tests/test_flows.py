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
