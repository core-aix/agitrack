"""A repository nested inside a tracked one is tracked SEPARATELY.

Two shapes, both built with real git: a submodule (``.git`` is a file pointing into the parent's
``.git/modules``) and an independent repo created in a subfolder (``git init`` / a clone dropped
there). Each has its own history, so its work is recorded by aGiTrack running in IT, and the
parent must neither stage it, nor count it as a change, nor list its sessions as the parent's.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agitrack import paths
from agitrack.git import GitRepo
from agitrack.git.hooks import hooks_dir_for_path


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text(f"{path.name}\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def parent(tmp_path) -> Path:
    """A parent repo with a submodule `sub/` and an independent nested repo `inner/`."""
    upstream = _repo(tmp_path / "upstream")
    root = _repo(tmp_path / "parent")
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "sub")
    _git(root, "commit", "-qm", "add sub")
    _repo(root / "inner")
    return root


def test_a_nested_repo_is_never_offered_for_staging_in_the_parent(parent):
    (parent / "new.txt").write_text("mine\n", encoding="utf-8")
    repo = GitRepo(parent)

    # The nested repo is one `inner/` entry in git's listing, never its files. Staging it would
    # commit it INTO the parent as an embedded gitlink — what the automatic commit used to do.
    assert repo.untracked_files() == ["new.txt"]
    assert repo.untracked_entries() == ["new.txt"]
    assert sorted(repo.nested_repo_paths()) == ["inner", "sub"]


def test_work_inside_a_nested_repo_is_not_a_change_to_the_parent(parent):
    repo = GitRepo(parent)
    assert repo.has_changes() is False  # the untracked nested repo alone is not a change

    (parent / "sub" / "f.txt").write_text("edited inside the submodule\n", encoding="utf-8")
    (parent / "inner" / "f.txt").write_text("edited inside the nested repo\n", encoding="utf-8")
    assert repo.has_changes() is False
    assert repo.has_tracked_changes() is False

    (parent / "f.txt").write_text("a real parent edit\n", encoding="utf-8")
    assert repo.has_changes() is True


def test_the_parents_snapshot_ignores_nested_repos_even_after_they_commit(parent):
    """The latent gate compares a snapshot with HEAD. The untracked nested repo used to be added
    as a gitlink HEAD never had — so an untouched parent read as changed forever — and a commit
    inside a submodule moved the snapshot, recording nested work as the parent's turn."""
    repo = GitRepo(parent)
    head = repo.comparable_tree("HEAD")
    assert repo.snapshot_worktree_tree() == head

    for nested in (parent / "sub", parent / "inner"):
        (nested / "f.txt").write_text("work\n", encoding="utf-8")
        _git(nested, "commit", "-qam", "nested work")
    assert repo.snapshot_worktree_tree() == head

    (parent / "f.txt").write_text("parent work\n", encoding="utf-8")
    assert repo.snapshot_worktree_tree() != head


def test_the_parents_latent_gate_records_nothing_for_nested_work(parent):
    from agitrack.commits import ManualCommitTracker
    from agitrack.config import AgitrackState

    repo = GitRepo(parent)
    tracker = ManualCommitTracker(repo, repo, AgitrackState(parent))
    (parent / "inner" / "f.txt").write_text("nested only\n", encoding="utf-8")
    assert tracker.gate() is False

    (parent / "f.txt").write_text("parent edit\n", encoding="utf-8")
    assert tracker.gate() is True


def test_each_repository_resolves_to_its_own_root_and_hooks(parent):
    assert GitRepo.discover(parent / "inner").repo.resolve() == (parent / "inner").resolve()
    assert GitRepo.discover(parent / "sub").repo.resolve() == (parent / "sub").resolve()
    # A submodule's `.git` is a file with a RELATIVE gitdir and no commondir.
    assert hooks_dir_for_path(parent / "sub") == (parent / ".git" / "modules" / "sub" / "hooks")
    assert hooks_dir_for_path(parent / "sub") == Path(GitRepo(parent / "sub").hooks_dir())
    assert hooks_dir_for_path(parent / "inner") == parent / "inner" / ".git" / "hooks"


def test_a_session_run_in_a_nested_repo_is_not_the_parents(parent):
    assert paths.in_nested_repo(parent, parent / "inner") is True
    assert paths.in_nested_repo(parent, parent / "sub" / "deep") is True
    assert paths.in_nested_repo(parent, parent / "src") is False  # a plain subfolder is the parent's
    assert paths.in_nested_repo(parent, parent) is False
    # A plain folder has no "nested" repos: a backtrace over it keeps every repo below it.
    assert paths.in_nested_repo(parent.parent, parent) is False


def test_claude_backtrace_listing_leaves_the_nested_repos_sessions_to_it(parent, tmp_path, monkeypatch):
    from agitrack.transcripts import claude

    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    for cwd, sid in (
        (parent, "11111111-1111-1111-1111-111111111111"),
        (parent / "inner", "22222222-2222-2222-2222-222222222222"),
    ):
        project = config / "projects" / claude._encode_repo(cwd.resolve())
        project.mkdir(parents=True)
        row = {
            "type": "user",
            "cwd": str(cwd.resolve()),
            "sessionId": sid,
            "message": {"role": "user", "content": "hi"},
        }
        (project / f"{sid}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    parent_sessions = {ref.id for ref, _ in claude.sessions_under(parent)}
    inner_sessions = {ref.id for ref, _ in claude.sessions_under(parent / "inner")}

    assert parent_sessions == {"11111111-1111-1111-1111-111111111111"}
    assert inner_sessions == {"22222222-2222-2222-2222-222222222222"}
