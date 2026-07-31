"""Direct tests for GitRepo working-tree queries (agitrack/git/repo.py)."""

from __future__ import annotations

import os
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


# --- git's comment char vs. aGiTrack's "#" section headings -----------------------------------


def _amend_through_an_editor(path) -> str:
    """Re-save the HEAD message through git's editor path (what `git commit --amend` does), then
    return the resulting message. GIT_EDITOR=true leaves the file untouched, so anything missing
    afterwards was removed by git's own message CLEANUP, not by an edit."""
    subprocess.run(["git", "commit", "-q", "--amend"], cwd=path, check=True, env={**os.environ, "GIT_EDITOR": "true"})
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=path, capture_output=True, text=True, check=True
    ).stdout


_AGITRACK_STYLE_MESSAGE = (
    "<aGiTrack> did the thing\n\n# Interaction Trace\n\n## User\n\nhello\n\n"
    "## Agent\n\ndone\n\n# aGiTrack Metadata\ncommit_type: agent\n"
)


def test_editing_a_commit_message_destroys_agitrack_headings_without_the_guard(tmp_path):
    # The failure this guards against: git's default `core.commentChar = "#"` makes its message
    # cleanup treat every aGiTrack section heading as a comment, so opening the message in an
    # editor (amend / rebase -i reword) silently deletes the whole trace + metadata structure and
    # the commit reads as untracked afterwards. Pinned so the guard below has a stated purpose.
    repo = _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("changed\n")
    repo.stage_paths(["f.txt"])
    repo.commit(_AGITRACK_STYLE_MESSAGE)

    message = _amend_through_an_editor(tmp_path)

    assert "# Interaction Trace" not in message  # every heading stripped...
    assert "# aGiTrack Metadata" not in message
    assert "## User" not in message
    assert "hello" in message and "done" in message  # ...while the prose survives, so it looks fine


def test_ensure_comment_char_preserves_headings_keeps_them_through_an_edit(tmp_path):
    repo = _init_repo(tmp_path)
    assert repo.ensure_comment_char_preserves_headings() is True
    (tmp_path / "f.txt").write_text("changed\n")
    repo.stage_paths(["f.txt"])
    repo.commit(_AGITRACK_STYLE_MESSAGE)

    message = _amend_through_an_editor(tmp_path)

    assert "# Interaction Trace" in message
    assert "# aGiTrack Metadata" in message
    assert message.count("## User") == 1 and message.count("## Agent") == 1


def test_ensure_comment_char_is_idempotent_and_repo_local(tmp_path):
    repo = _init_repo(tmp_path)
    assert repo.ensure_comment_char_preserves_headings() is True
    # Written to the REPO's config, never the user's global one.
    local = subprocess.run(
        ["git", "config", "--local", "--get", "core.commentChar"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert local == "auto"
    # A second call is a no-op (already set) rather than rewriting it.
    assert repo.ensure_comment_char_preserves_headings() is False


def test_ensure_comment_char_never_overrides_a_value_the_user_chose(tmp_path):
    repo = _init_repo(tmp_path)
    subprocess.run(["git", "config", "--local", "core.commentChar", ";"], cwd=tmp_path, check=True)

    assert repo.ensure_comment_char_preserves_headings() is False

    kept = subprocess.run(
        ["git", "config", "--local", "--get", "core.commentChar"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert kept == ";"  # the user's choice stands (and it already protects "#" headings)
