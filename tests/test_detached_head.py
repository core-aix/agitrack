"""Detached HEAD (B8): aGiTrack must SAY the repo has no branch checked out."""

from __future__ import annotations

import subprocess

from agitrack.git import GitRepo
from agitrack.proxy.runner import ProxyRunner


def _repo_with_two_commits(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    for text in ("one\n", "two\n"):
        (root / "a.txt").write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", text.strip()], cwd=root, check=True)
    return GitRepo.discover(root)


def _runner_for(repo):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = repo
    runner._debug = lambda message: None
    return runner


def test_a_detached_head_is_announced_before_the_agent_starts(tmp_path, capsys):
    """The only trace of a detached HEAD anywhere in the UI was the status bar rendering
    "-> HEAD". Integration then fast-forwarded that detached HEAD onto the aGiTrack commit
    without asking, leaving `main` untouched and the turn branch deleted — so the work was
    reflog-only from the next `git checkout main` onward."""
    repo = _repo_with_two_commits(tmp_path)
    head = repo.rev_parse("HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=repo.repo, check=True)

    _runner_for(repo)._warn_if_base_is_detached()

    out = capsys.readouterr().out
    assert "DETACHED HEAD" in out
    assert head[:12] in out
    assert "git switch -c" in out  # a command that actually keeps the work


def test_an_ordinary_branch_says_nothing(tmp_path, capsys):
    repo = _repo_with_two_commits(tmp_path)

    _runner_for(repo)._warn_if_base_is_detached()

    assert capsys.readouterr().out == ""
