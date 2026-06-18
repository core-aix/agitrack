"""Headless recovery of work left by a session that exited abruptly.

These exercise agitrack.recovery.RecoveryService against real git repositories +
worktrees, with the backend transcript faked (so no agent runs): the policy is
driven entirely by the transcript's latest-turn state, which is exactly what the
fake controls.
"""

from __future__ import annotations

import os

import pytest

from agitrack.backends.base import TokenUsage
from agitrack.config import AgitrackState
from agitrack.git import GitRepo, RepoLock
from agitrack.git.worktree import WorktreeManager
from agitrack.recovery import RecoveryService
from agitrack.transcripts.types import ExportedSession, SessionTurn

pytestmark = pytest.mark.skipif(os.name != "posix", reason="aGiTrack is POSIX-only")


class _Config:
    """Minimal stand-in for GlobalConfig: backend default + summarization off (so
    recovery never makes an LLM call in tests)."""

    default_backend = "claude"
    summarization_enabled = False
    summarization_model = None


class _FakeBackend:
    def __init__(self, exported: ExportedSession | None) -> None:
        self._exported = exported

    def export_session(self, _repo, _session_id):
        return self._exported


def _exported(*, complete: bool, final: str = "did the thing") -> ExportedSession:
    turn = SessionTurn(
        user_message_id="u1",
        assistant_message_id="a1",
        user_prompt="please do the thing",
        final_response=final,
        tokens=TokenUsage(),
        model="claude-x",
        complete=complete,
    )
    return ExportedSession(session_id="ses-1", model="claude-x", updated=None, turns=[turn])


def _base_with_worktree(tmp_path, name="sess1"):
    base = GitRepo.init(tmp_path)  # seeds an initial commit
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    base.stage_paths(["seed.txt"])
    base.commit("seed")
    manager = WorktreeManager(base)
    info = manager.create(name, base=base.current_branch())
    wt = GitRepo(info.path)
    state = AgitrackState(info.path, default_backend="claude")
    state.backend_session_id = "ses-1"
    return base, manager, info, wt, state


def _patch_backend(monkeypatch, exported):
    monkeypatch.setattr("agitrack.recovery.make_proxy_agent", lambda _name: _FakeBackend(exported))


def test_finished_turn_is_committed_and_merged(tmp_path, monkeypatch):
    base, _manager, info, wt, _state = _base_with_worktree(tmp_path)
    # The agent's (uncommitted) work and a transcript whose latest turn is complete.
    (info.path / "feature.py").write_text("print('hi')\n", encoding="utf-8")
    _patch_backend(monkeypatch, _exported(complete=True))

    report = RecoveryService(base, _Config()).recover()

    assert report.recovered == ["sess1"]
    assert report.integrated == ["sess1"]
    assert report.flagged == []
    # The finished turn's file is now committed AND merged into the base branch...
    assert (tmp_path / "feature.py").read_text(encoding="utf-8") == "print('hi')\n"
    # ...and the session worktree was removed.
    assert not info.path.exists()


class _FakeSummarizer:
    model = "sum-model"
    tokens_input = 10
    tokens_output = 5
    tokens_cache_read = 0

    def summarize_commit(self, *, trace):
        return "RECOVERED-SUMMARY: did the thing"

    def update_session_summary(self, *, current_summary, trace, commit_summary):
        return "SESSION-SUMMARY"


def test_finished_turn_is_summarized_when_enabled(tmp_path, monkeypatch):
    base, _manager, info, _wt, _state = _base_with_worktree(tmp_path)
    (info.path / "feature.py").write_text("print('hi')\n", encoding="utf-8")
    _patch_backend(monkeypatch, _exported(complete=True))
    # Run the real summarize/amend path with a stubbed summarizer (no LLM call).
    monkeypatch.setattr(RecoveryService, "_make_summarizer", lambda _self, _state: _FakeSummarizer())
    config = _Config()
    config.summarization_enabled = True

    report = RecoveryService(base, config).recover()

    assert report.recovered == ["sess1"]
    assert report.integrated == ["sess1"]
    # The summary was folded into the now-integrated commit message...
    assert "RECOVERED-SUMMARY" in base.commit_message("HEAD")
    # ...and recorded as a git note on it.
    note = base.notes_show(base.rev_parse("HEAD"), namespace="agitrack/commit-summary")
    assert "RECOVERED-SUMMARY" in (note or "")


def test_aborted_turn_is_left_untouched(tmp_path, monkeypatch):
    base, _manager, info, wt, _state = _base_with_worktree(tmp_path)
    (info.path / "half.py").write_text("incomplete\n", encoding="utf-8")
    _patch_backend(monkeypatch, _exported(complete=False))  # latest turn still in flight

    report = RecoveryService(base, _Config()).recover()

    assert report.recovered == []
    assert report.flagged == ["sess1"]  # flagged for the user, not committed
    # No commit was made: the worktree and its uncommitted change survive.
    assert info.path.exists()
    assert (info.path / "half.py").exists()
    assert wt.has_changes()  # still uncommitted
    assert not (tmp_path / "half.py").exists()  # nothing leaked into the base


def test_committed_but_unmerged_work_is_integrated(tmp_path, monkeypatch):
    base, _manager, info, wt, _state = _base_with_worktree(tmp_path)
    # A previous turn already committed on a managed turn branch, but was never
    # merged (e.g. killed right after the commit). No uncommitted changes remain.
    wt.switch("agitrack/claude/sess1/t1", create=True)
    (info.path / "done.py").write_text("complete\n", encoding="utf-8")
    wt.stage_paths(["done.py"])
    wt.commit("agent turn")
    _patch_backend(monkeypatch, _exported(complete=True))

    report = RecoveryService(base, _Config()).recover()

    assert report.recovered == []  # nothing to commit; it was already committed
    assert report.integrated == ["sess1"]
    assert (tmp_path / "done.py").read_text(encoding="utf-8") == "complete\n"
    assert not info.path.exists()


def test_recovery_skips_when_a_live_session_holds_the_lock(tmp_path, monkeypatch):
    base, _manager, info, _wt, _state = _base_with_worktree(tmp_path)
    (info.path / "feature.py").write_text("x\n", encoding="utf-8")
    _patch_backend(monkeypatch, _exported(complete=True))
    # A live aGiTrack still holds the repo lock.
    holder = RepoLock(base.repo / ".agitrack" / "lock")
    assert holder.acquire() is True
    try:
        report = RecoveryService(base, _Config()).recover()
    finally:
        holder.release()

    assert report.skipped_busy is True
    assert "already running" in report.summary().lower()
    assert info.path.exists()  # did nothing


def test_nothing_to_recover_with_no_worktrees(tmp_path):
    base = GitRepo.init(tmp_path)
    report = RecoveryService(base, _Config()).recover()
    assert report.did_work() is False
    assert report.summary() == "Nothing to recover."
