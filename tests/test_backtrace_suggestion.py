"""When the live dashboard would be empty but a reconstruction would not, show the reconstruction.

A repo only has live-dashboard history once aGiTrack has committed in it, so someone who coded
with Claude/OpenCode BEFORE adopting aGiTrack gets an empty page — while their own transcripts
hold history `--backtrace` can reconstruct. Showing "nothing to see" in that situation is the
worst answer available, so the dashboard defers to the reconstruction.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agitrack.commits import METADATA_HEADER
from agitrack.git import GitRepo
from agitrack.metrics import suggest


def _repo(path: Path, *, tracked: bool, tokens: int = 0) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    message = "plain human commit"
    if tracked:
        message = f"<aGiTrack> did the thing\n\n{METADATA_HEADER}\ncommit_type: agent\n"
        if tokens:
            message += f"tokens_since_last_commit_output: {tokens}\n"
    subprocess.run(["git", "-C", str(path), "commit", "-q", "--cleanup=verbatim", "-m", message], check=True)
    return GitRepo(path)


def test_a_repo_with_agitrack_history_is_never_diverted(tmp_path, monkeypatch):
    # The overwhelmingly common case, and the one that must stay fast: once anything is tracked
    # the decision is a single `git log` and the session probe never runs at all.
    repo = _repo(tmp_path, tracked=True, tokens=120)
    monkeypatch.setattr(
        suggest, "has_backtrace_history", lambda _d: pytest.fail("must not probe sessions when tokens exist")
    )

    assert suggest.has_tracked_history(repo) is True
    assert suggest.has_tracked_tokens(repo) is True
    assert suggest.should_show_backtrace(repo) is False


def test_an_untracked_repo_with_local_sessions_is_diverted(tmp_path, monkeypatch):
    repo = _repo(tmp_path, tracked=False)
    monkeypatch.setattr(suggest, "has_backtrace_history", lambda _d: True)

    assert suggest.has_tracked_history(repo) is False
    assert suggest.should_show_backtrace(repo) is True


def test_an_untracked_repo_with_no_sessions_is_not_diverted(tmp_path, monkeypatch):
    # Nothing to reconstruct either, so the live dashboard (and its empty state, which explains
    # how to get started) is the right answer.
    repo = _repo(tmp_path, tracked=False)
    monkeypatch.setattr(suggest, "has_backtrace_history", lambda _d: False)

    assert suggest.should_show_backtrace(repo) is False


def test_history_tracked_on_another_branch_still_counts(tmp_path):
    # `--all`: a user whose tracked work sits on a feature branch must not be told their history
    # is empty and sent to a reconstruction of it.
    repo = _repo(tmp_path, tracked=False)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-qb", "feature"], check=True)
    (tmp_path / "b.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "commit",
            "-q",
            "--cleanup=verbatim",
            "-m",
            f"<aGiTrack> t\n\n{METADATA_HEADER}\ncommit_type: agent\n",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", repo.current_branch() or "master"], check=False)

    assert suggest.has_tracked_history(repo) is True


def test_a_failed_probe_never_diverts(tmp_path, monkeypatch):
    # Both probes fail SAFE in opposite directions, so a broken probe can only ever leave the user
    # with the dashboard they asked for — never silently redirect them somewhere else.
    repo = _repo(tmp_path, tracked=False)

    def boom(*_a, **_k):
        raise OSError("git is unavailable")

    monkeypatch.setattr(repo, "_run", boom)
    assert suggest.has_tracked_history(repo) is True  # assume tracked ⇒ no diversion

    monkeypatch.setattr("agitrack.metrics.backtrace._discover", lambda _d: (_ for _ in ()).throw(RuntimeError("boom")))
    assert suggest.has_backtrace_history(tmp_path) is False  # assume nothing to show ⇒ no diversion


def test_the_notices_say_what_the_user_gains_by_committing_through_agitrack(tmp_path):
    # The point of the message is not "this is not the dashboard" but "here is what tracking buys
    # you" — the reconstruction INFERS the link from conversation to code; aGiTrack RECORDS it.
    for text in (suggest.SUBSTITUTION_NOTICE, suggest.STARTUP_HINT):
        assert "backtrace" in text.lower()
        assert "recorded" in text.lower() and ("infer" in text.lower() or "reconstruct" in text.lower())
