"""Background (headless) mode: aGiTrack tracks a user-driven native backend session without the
interactive TUI. Always no-worktree; manual (latent + fold on the user's commit) or auto (aGiTrack
folds the pending turns into a commit itself). Reuses the same CommitEngine + ManualCommitTracker
as the proxy, so token/turn accounting is identical."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.commits import ManualCommitTracker
from agitrack.config import AgitrackState
from agitrack.config.settings import GlobalConfig
from agitrack.git import GitRepo
from agitrack.proxy.background import BackgroundRunner
from agitrack.transcripts.types import ExportedSession, SessionTurn


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def _git(repo: GitRepo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo.repo), *args], capture_output=True, text=True, check=True).stdout


class FakeBackend:
    name = "claude"

    def __init__(self) -> None:
        self.sessions: dict[str, ExportedSession] = {}
        self.latest: str | None = None

    def latest_session_id(self, repo):
        return self.latest

    def export_session(self, repo, session_id):
        return self.sessions.get(session_id)

    def set_session(self, session_id: str, turns: list[SessionTurn], *, model: str = "claude-opus-4-8") -> None:
        self.sessions[session_id] = ExportedSession(session_id, model, None, turns)
        self.latest = session_id


def _turn(uid: str, aid: str, prompt: str, response: str, out: int) -> SessionTurn:
    return SessionTurn(uid, aid, prompt, response, TokenUsage(total=out, output=out), "claude-opus-4-8")


def _runner(tmp_path, *, manual: bool):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    gc = GlobalConfig(path=tmp_path / "gc.json")
    runner = BackgroundRunner(repo, manual_commits=manual, _global_config=gc, _state=state)
    runner.backend = FakeBackend()
    runner._make_summarizer = lambda: None  # never spawn a real summarizer LLM in tests
    return runner, repo, state, runner.backend


# --- manual mode ------------------------------------------------------------


def test_background_manual_records_latent_and_freezes_head(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    head = repo.rev_parse("HEAD")
    # The user drives the agent: it edits a file and the transcript records a completed turn.
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    assert runner._process_once() is True
    assert repo.rev_parse("HEAD") == head  # HEAD never moves in manual mode
    assert repo.ref_sha(runner._manual.ref()) is not None  # recorded on the latent ref
    # The fold trailer the prepare-commit-msg hook reads was rendered.
    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()


def test_background_manual_folds_into_user_commit_via_hook(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    # The user commits their work (the fold + reset hooks are installed).
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "my work")

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "my work" in msg
    assert msg.count("# aGiTrack Metadata") == 2  # user block + the one folded turn
    assert len(_git(repo, "log", "--format=%H").split()) == 2  # init + one folded commit


# --- auto mode --------------------------------------------------------------


def test_background_auto_folds_pending_into_a_commit_itself(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()  # records the latent turn

    runner._auto_fold_pending()  # aGiTrack commits it itself (no user action)

    log = _git(repo, "log", "--format=%H").split()
    assert len(log) == 2  # init + the auto commit
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "# aGiTrack Metadata" in msg and "do x" in msg
    # Ref reset to HEAD, so nothing is pending after the auto commit.
    assert repo.ref_sha(runner._manual.ref()) == repo.rev_parse("HEAD")
    assert runner._manual.pending_count() == 0


def test_background_auto_skips_when_agent_committed_its_own_work(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()
    before = repo.rev_parse("HEAD")
    # The agent commits its own work (the fold hook folded tracking into it, resetting the ref).
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent commit")
    runner._manual.service()  # react to the commit: drop the now-stale latent chain

    runner._auto_fold_pending()  # clean tree ⇒ nothing more to do

    # Only the agent's own commit was added — aGiTrack did NOT add a second commit on top.
    assert _git(repo, "log", "--format=%H").split()[0] != before
    assert len(_git(repo, "log", "--format=%H").split()) == 2


# --- discovery / accounting -------------------------------------------------


def test_background_follows_latest_session_and_counts_once(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    # First conversation, one turn.
    (tmp_path / "a.txt").write_text("one\nfirst\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "first", "done", 10)])
    assert runner._process_once() is True
    # Re-processing the SAME transcript records nothing new (watermark already past it).
    assert runner._process_once() is False

    # The user switches to a new conversation inside the backend; aGiTrack follows the latest.
    (tmp_path / "a.txt").write_text("one\nfirst\nsecond\n", encoding="utf-8")
    backend.set_session("s2", [_turn("u2", "m2", "second", "done", 15)])
    assert runner._process_once() is True
    assert state.backend_session_id == "s2"  # followed the switch
    # Two latent turns pending (one per conversation), each counted once.
    assert runner._manual.pending_count() == 2


# --- ManualCommitTracker direct --------------------------------------------


def test_manual_tracker_gate_and_record(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    head = repo.rev_parse("HEAD")

    assert tracker.gate() is False  # clean tree ⇒ nothing to record
    (tmp_path / "a.txt").write_text("one\nx\n", encoding="utf-8")
    assert tracker.gate() is True
    sha = tracker.record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: agent\n")

    assert sha is not None
    assert repo.rev_parse("HEAD") == head  # HEAD frozen
    assert repo.ref_sha(tracker.ref()) is not None
    assert tracker.pending_count() == 1


def test_manual_tracker_reconcile_covers_external_commit(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    tracker.hooks_installed = False  # force the cover fallback
    tracker.last_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    tracker.gate()
    tracker.record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: agent\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "external")
    user_head = repo.rev_parse("HEAD")
    user_tree = repo.rev_parse("HEAD^{tree}")

    tracker.reconcile_external_commit()

    cover = repo.rev_parse("HEAD")
    assert cover != user_head  # a cover commit was added
    assert repo.parents(cover)[0] == user_head  # first-parent = the user's own commit
    assert repo.rev_parse("HEAD^{tree}") == user_tree  # cover introduced no diff
    assert "# aGiTrack Metadata" in repo.commit_message(cover)
    assert repo.ref_sha(tracker.ref()) == cover  # ref reset
