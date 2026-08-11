"""Direct tests for GitRepo working-tree queries (agitrack/git/repo.py)."""

from __future__ import annotations

import os
import subprocess

from agitrack.git import GitRepo, read_cache


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


# --- non-ASCII path names ---------------------------------------------------------------------


def test_a_non_ascii_filename_round_trips_from_listing_back_into_the_index(tmp_path):
    # git's DEFAULT core.quotePath=true prints a path holding non-ASCII characters C-quoted —
    # `"caf\303\251 \344\270\255.txt"`, surrounding double quotes included. aGiTrack feeds the
    # names it lists straight back to `git add --`, which rejects the quoted form ("pathspec ...
    # did not match any files"); staging then fails and the ENTIRE agent turn is lost — no
    # commit, no interaction trace, no token accounting. Any agent creating an accented or CJK
    # filename hit this, so the listing must come back verbatim.
    repo = _init_repo(tmp_path)
    name = "café 中 file.txt"
    (tmp_path / name).write_text("hello\n", encoding="utf-8")

    listed = repo.untracked_files()

    assert listed == [name]
    repo.stage_paths(listed)
    assert repo.staged_paths() == [name]


def test_a_non_ascii_filename_is_not_quoted_in_the_short_status(tmp_path):
    # The same quoting reaches `status --short`, which aGiTrack shows to the user and parses.
    repo = _init_repo(tmp_path)
    (tmp_path / "ünïcödé.txt").write_text("x\n", encoding="utf-8")

    assert "ünïcödé.txt" in repo.status_short()
    assert "\\303" not in repo.status_short()


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


# --- the short-lived read cache (repo.read_cache) ----------------------------------------
#
# WHY THIS IS TESTED HARD: it is the only place in aGiTrack where a git answer can be older
# than the question. Every test below is about the invalidation, not the speedup — a cache
# that serves a stale "the tree is clean" is how uncommitted user work gets silently dropped.


def _count_git_spawns(monkeypatch, repo):
    """Record every git subprocess GitRepo actually spawns."""
    import agitrack.git.repo as repo_module

    spawned: list[tuple] = []
    real_run = repo_module.subprocess.run

    def counting_run(command, **kwargs):
        spawned.append(tuple(command))
        return real_run(command, **kwargs)

    monkeypatch.setattr(repo_module.subprocess, "run", counting_run)
    return spawned


def test_read_cache_answers_a_repeated_read_once(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    spawned = _count_git_spawns(monkeypatch, repo)

    with read_cache():
        first = repo.status_short()
        second = repo.status_short()
        branch_reads = [repo.current_branch() for _ in range(3)]

    assert first == second
    assert len(set(branch_reads)) == 1
    assert sum(1 for cmd in spawned if "status" in cmd) == 1
    assert sum(1 for cmd in spawned if "rev-parse" in cmd) == 1


def test_nothing_is_cached_outside_a_scope(tmp_path, monkeypatch):
    # The cache must be opt-in per call site. A GitRepo used anywhere else in aGiTrack —
    # the dashboard, the background daemon, the metrics scan — has to keep seeing live data.
    repo = _init_repo(tmp_path)
    spawned = _count_git_spawns(monkeypatch, repo)

    repo.status_short()
    repo.status_short()

    assert sum(1 for cmd in spawned if "status" in cmd) == 2


def test_a_write_inside_the_scope_invalidates_the_read_before_it(tmp_path):
    """The property everything else rests on: read -> write -> read sees the write.

    `_offer_pre_agent_user_commit` commits the user's edits in the middle of the submit path,
    and every check after it must see the now-clean tree. A cache that served the pre-commit
    status there would have aGiTrack act on changes that no longer exist.
    """
    repo = _init_repo(tmp_path)
    (tmp_path / "new.txt").write_text("hi\n")

    with read_cache():
        assert "new.txt" in repo.status_short()
        repo.stage_paths(["new.txt"])
        repo.commit("add new.txt")
        assert repo.status_short().strip() == ""  # NOT the cached dirty answer


def test_a_write_on_another_thread_invalidates_the_cache(tmp_path):
    """The git worker thread commits on the very repo the reactor is reading.

    The cache is thread-local (so one thread can never serve another's answers) but
    invalidation is process-wide, precisely so a commit made on the worker thread is a miss
    here rather than a stale hit. Without the shared write epoch this is exactly the race
    that would make the submit path decide against a tree that no longer exists.
    """
    import threading

    repo = _init_repo(tmp_path)
    (tmp_path / "new.txt").write_text("hi\n")

    with read_cache():
        assert "new.txt" in repo.status_short()

        def worker():
            other = GitRepo.discover(tmp_path)  # a separate GitRepo, as the worker really has
            other.stage_paths(["new.txt"])
            other.commit("committed elsewhere")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert repo.status_short().strip() == ""


def test_the_cache_is_dropped_when_the_scope_closes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    with read_cache():
        repo.status_short()
    (tmp_path / "later.txt").write_text("hi\n")

    assert "later.txt" in repo.status_short()


def test_nested_scopes_share_one_cache_and_survive_to_the_outermost(tmp_path, monkeypatch):
    # `_resume_pending_prompt_if_ready` can run inside a scope the submit path already
    # opened; a nested scope that cleared on its own exit would silently halve the benefit.
    repo = _init_repo(tmp_path)
    spawned = _count_git_spawns(monkeypatch, repo)

    with read_cache():
        repo.status_short()
        with read_cache():
            repo.status_short()
        repo.status_short()

    assert sum(1 for cmd in spawned if "status" in cmd) == 1
