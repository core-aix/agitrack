"""Direct tests for GitRepo working-tree queries (agitrack/git/repo.py)."""

from __future__ import annotations

import subprocess

from agitrack.git import GitRepo


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def test_untracked_entries_ignores_dirs_ignored_only_by_a_nested_gitignore(tmp_path):
    # A directory ignored solely from WITHIN (a nested .gitignore with `*`, as venv/ruff/npm
    # caches drop) has no rule matching the directory itself, so `git ls-files --others
    # --directory` collapses it and reports it as untracked even though every file inside is
    # ignored. `git status` shows nothing, so aGiTrack must not raise a phantom staging prompt.
    repo = _init_repo(tmp_path)
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / ".gitignore").write_text("*\n")  # self-ignore everything, including this file
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    (venv / "bin").mkdir()
    (venv / "bin" / "python").write_text("#!/bin/sh\n")

    # git itself sees a clean tree...
    assert repo.status_short().strip() == ""
    assert repo.untracked_files() == []
    # ...and so must aGiTrack's collapsed view (the bug listed ".venv/" here).
    assert repo.untracked_entries() == []


def test_untracked_entries_ignores_empty_directories(tmp_path):
    # git cannot track an empty directory, so it must never be offered for staging — but
    # `--directory` reports it. Cross-checking against the per-file list drops it.
    repo = _init_repo(tmp_path)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "content" / "about").mkdir(parents=True)

    assert repo.untracked_entries() == []


def test_untracked_entries_still_reports_a_genuinely_untracked_directory(tmp_path):
    # The collapse must be preserved for a real new directory: it comes back as ONE `dir/`
    # entry (so declining it once covers files added later), not file-by-file.
    repo = _init_repo(tmp_path)
    feature = tmp_path / "feature"
    feature.mkdir()
    (feature / "a.py").write_text("x = 1\n")
    (feature / "b.py").write_text("y = 2\n")
    (tmp_path / "loose.txt").write_text("hi\n")  # a top-level untracked file stays listed too

    entries = set(repo.untracked_entries())
    assert "feature/" in entries  # collapsed to one dir entry
    assert "loose.txt" in entries
    assert "feature/a.py" not in entries  # not expanded file-by-file


def test_untracked_entries_reports_untracked_files_in_a_partially_tracked_dir(tmp_path):
    # A directory that ALSO holds tracked files can't be collapsed by git; its individual
    # untracked files must still surface (and survive the cross-check).
    repo = _init_repo(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "tracked.py").write_text("t = 1\n")
    repo.stage_paths(["pkg/tracked.py"])
    repo.commit("add pkg")
    (pkg / "new.py").write_text("n = 1\n")

    assert repo.untracked_entries() == ["pkg/new.py"]
