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


def test_a_plain_RESTART_recovers_what_agitrack_recover_would(tmp_path, monkeypatch):
    # Found live: after a SIGKILL, `agitrack --recover` committed and merged the crashed
    # session's finished turn, while simply relaunching aGiTrack left the same turn
    # uncommitted behind "⚠ 1 stale session(s) need attention". Same crash, different outcome
    # depending on how the user came back — although this module's docstring calls the
    # launch-time path the lazy form of exactly this recovery. Startup only ever integrated
    # work that was already COMMITTED.
    from proxy_helpers import make_runner

    base, manager, info, _wt, _state = _base_with_worktree(tmp_path, name="crashed")
    (info.path / "feature.py").write_text("print('hi')\n", encoding="utf-8")  # uncommitted turn work
    _patch_backend(monkeypatch, _exported(complete=True))
    live = manager.create("live-session", base=base.current_branch())  # the session being started now

    runner = make_runner(
        repo=GitRepo(live.path),
        base_repo=base,
        _base_branch=base.current_branch(),
        worktree=live,
        name="live-session",
    )
    runner.worktree_manager = manager
    runner.global_config = _Config()
    messages: list[str] = []
    runner._set_message = lambda message, **kw: messages.append(message)
    runner._debug = lambda *a, **k: None

    runner._reconcile_sessions_on_startup()

    assert (tmp_path / "feature.py").read_text(encoding="utf-8") == "print('hi')\n"  # committed AND merged
    assert not any("need attention" in m for m in messages)  # nothing left for the user to chase


def test_a_restart_still_flags_a_turn_that_was_never_finished(tmp_path, monkeypatch):
    # The policy's other half must survive: a turn still in flight when the process died is
    # never committed — it is left exactly as it is and reported.
    from proxy_helpers import make_runner

    base, manager, info, _wt, _state = _base_with_worktree(tmp_path, name="crashed")
    (info.path / "half.py").write_text("incomplete\n", encoding="utf-8")
    _patch_backend(monkeypatch, _exported(complete=False))
    live = manager.create("live-session", base=base.current_branch())

    runner = make_runner(
        repo=GitRepo(live.path),
        base_repo=base,
        _base_branch=base.current_branch(),
        worktree=live,
        name="live-session",
    )
    runner.worktree_manager = manager
    runner.global_config = _Config()
    messages: list[str] = []
    runner._set_message = lambda message, **kw: messages.append(message)
    runner._debug = lambda *a, **k: None

    runner._reconcile_sessions_on_startup()

    assert (info.path / "half.py").exists()  # untouched
    assert not (tmp_path / "half.py").exists()  # never merged into the base
    assert any("crashed" in m for m in messages)  # and surfaced


def test_a_crashed_run_resumes_its_own_session_instead_of_starting_a_random_new_one(tmp_path):
    # Found live: SIGKILL a worktree session, relaunch, and aGiTrack came back as a brand-new
    # randomly-named session ("ember") with a fresh conversation, while the real one sat under
    # .agitrack/worktrees/alpha. No work was lost — the identity was. Both records that normally
    # answer "what do I resume?" die with the process: the root pointer is written only by the
    # graceful exit path, and the repo scan looks in the BASE project dir while a worktree
    # session's conversation is keyed by the WORKTREE path.
    from proxy_helpers import make_runner

    base = GitRepo.init(tmp_path)
    runner = make_runner(repo=base, base_repo=base)
    runner._repo_latest_session_id = lambda: None  # nothing in the base project dir
    runner._newest_worktree_session = lambda: ("ses-from-the-worktree", 123.0)
    root_state = AgitrackState(tmp_path, default_backend="claude")  # crash state: no pointer

    assert root_state.backend_session_id is None
    assert runner._startup_resume_id(root_state) == "ses-from-the-worktree"

    # …and with a conversation to key it to, the name the crashed run chose is recovered too.
    root_state.remember_pending_session_name("alpha")
    assert runner._adopt_pending_session_name(root_state, "ses-from-the-worktree") == "alpha"


def test_the_recorded_resume_pointer_still_wins_when_there_is_one(tmp_path):
    # A graceful exit's pointer must not be overridden by a merely-newer worktree transcript.
    from proxy_helpers import make_runner

    base = GitRepo.init(tmp_path)
    runner = make_runner(repo=base, base_repo=base)
    runner._repo_latest_session_id = lambda: "from-the-repo-scan"
    runner._newest_worktree_session = lambda: ("from-the-worktree", 999.0)
    root_state = AgitrackState(tmp_path, default_backend="claude")
    root_state.backend_session_id = "the-session-i-quit-in"

    assert runner._startup_resume_id(root_state) == "the-session-i-quit-in"


def test_recover_explains_a_no_worktree_crash_instead_of_saying_nothing_to_recover(tmp_path):
    """C35: after a --no-worktree crash this printed "Nothing to recover." — BYTE-IDENTICAL to
    the never-tracked case — and the agent's work stayed uncommitted forever, while `--help`
    promised recovery unconditionally. The behaviour is deliberate (`recovery.py`: worktree
    sessions only); the silence was not."""
    import subprocess

    from agitrack.config import GlobalConfig
    from agitrack.git import GitRepo
    from agitrack.recovery import RecoveryService

    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    # What a --no-worktree crash leaves: the agent's edit, live in the base tree.
    (root / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "hello.txt"], cwd=root, check=True)

    summary = RecoveryService(GitRepo.discover(root), GlobalConfig()).recover().summary()

    assert "Nothing to recover automatically" in summary
    assert "--no-worktree" in summary
    assert "git status" in summary  # a next step the user can actually take


def test_a_genuinely_empty_repo_still_says_nothing_to_recover(tmp_path):
    import subprocess

    from agitrack.config import GlobalConfig
    from agitrack.git import GitRepo
    from agitrack.recovery import RecoveryService

    root = tmp_path / "clean"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    assert RecoveryService(GitRepo.discover(root), GlobalConfig()).recover().summary() == "Nothing to recover."
