"""Issue #296: a manual-commit start asks what to do with turns an earlier run left pending."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from agitrack import cli, tracking_gap
from agitrack.commits.manual import ManualCommitTracker
from agitrack.config import AgitrackState
from agitrack.git import GitRepo


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def _record(repo: GitRepo, session_id: str, name: str) -> None:
    state = AgitrackState(repo.repo, default_backend="claude")
    state.data["agitrack_session_id"] = session_id
    ManualCommitTracker(repo, repo, state).record(
        f"<aGiTrack> {name}\n\n# Interaction Trace\n\n## User\n\n{name} prompt\n\n"
        "# aGiTrack Metadata\ncommit_type: agent\ntokens_since_last_commit_output: 7\n"
    )


def _pending(repo: GitRepo) -> int:
    return ManualCommitTracker(repo, repo, AgitrackState(repo.repo, default_backend="claude")).pending_count()


def _interactive(monkeypatch, answer: str | None) -> list[str]:
    asked: list[str] = []
    monkeypatch.setattr(cli, "stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "stdout_is_interactive", lambda: True)

    def ask(question: str) -> str:
        asked.append(question)
        if answer is None:
            raise AssertionError("should not have asked")
        return answer

    monkeypatch.setattr(cli, "_ask", ask)
    return asked


def test_discarding_leftovers_drops_every_sessions_pending_turns(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    # Two sessions' worth: a backend switch mints a new session id, and both refs are leftovers.
    _record(repo, "old-session", "first")
    _record(repo, "other-session", "second")
    assert _pending(repo) == 2

    asked = _interactive(monkeypatch, "n")
    cli._offer_leftover_manual_traces(repo, scripted=False)

    assert len(asked) == 1
    assert _pending(repo) == 0
    # The fold file the commit hook reads holds none of the discarded text.
    trailer = (tmp_path / ".agitrack" / "manual-pending-trailer").read_text(encoding="utf-8")
    assert "first prompt" not in trailer and "second prompt" not in trailer


def test_discarding_also_keeps_unrecorded_earlier_turns_out(tmp_path, monkeypatch):
    # A turn that was running when the machine went down was never recorded at all; it lives
    # only in the transcript. Discarding moves the floor, so the next tracker skips it too.
    repo = _init_repo(tmp_path)
    _record(repo, "old-session", "first")
    _interactive(monkeypatch, "no")
    before = int(time.time()) - 60
    cli._offer_leftover_manual_traces(repo, scripted=False)

    floor = tracking_gap.tracking_floor(repo.repo)
    assert floor is not None and floor >= before + 60 - 1
    assert not tracking_gap.gap_is_open(repo.repo)
    old = SimpleNamespace(started_at=before, ended_at=before + 5)
    new = SimpleNamespace(started_at=int(floor) + 5, ended_at=int(floor) + 9)
    untracked, tracked = tracking_gap.split_untracked_turns([old, new], floor)
    assert untracked == [old] and tracked == [new]


def test_discarding_ends_an_open_stop_like_any_user_start(tmp_path):
    repo = _init_repo(tmp_path)
    tracking_gap.mark_stopped(repo.repo, now=1000.0)
    assert tracking_gap.gap_is_open(repo.repo)
    assert tracking_gap.discard_before(repo.repo, now=2000.0) == 2000.0
    assert not tracking_gap.gap_is_open(repo.repo)
    assert tracking_gap.tracking_floor(repo.repo) == 2000.0


def test_keeping_leftovers_is_the_default(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _record(repo, "old-session", "first")
    asked = _interactive(monkeypatch, "")
    cli._offer_leftover_manual_traces(repo, scripted=False)
    assert len(asked) == 1
    assert _pending(repo) == 1
    assert tracking_gap.tracking_floor(repo.repo) is None


def test_nothing_pending_asks_nothing(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _interactive(monkeypatch, None)
    cli._offer_leftover_manual_traces(repo, scripted=False)


def test_a_start_without_a_terminal_keeps_them_without_asking(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _record(repo, "old-session", "first")
    _interactive(monkeypatch, None)
    monkeypatch.setattr(cli, "stdin_is_interactive", lambda: False)
    cli._offer_leftover_manual_traces(repo, scripted=False)
    cli._offer_leftover_manual_traces(repo, scripted=True)
    assert _pending(repo) == 1


def test_turns_a_commit_already_holds_are_not_leftovers(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _record(repo, "old-session", "first")
    tracker = ManualCommitTracker(repo, repo, AgitrackState(repo.repo, default_backend="claude"))
    tracker.mark_folded()  # what the post-commit hook does after folding them
    _interactive(monkeypatch, None)
    cli._offer_leftover_manual_traces(repo, scripted=False)
