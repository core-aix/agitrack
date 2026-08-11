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
    # NOT "auto": git 2.54 deprecates it and warns on every commit forever (see
    # ensure_comment_char_preserves_headings).
    assert local not in ("auto", "#") and local
    # A second call is a no-op (already set) rather than rewriting it.
    assert repo.ensure_comment_char_preserves_headings() is False


def test_ensure_comment_char_never_overrides_a_value_the_user_chose(tmp_path):
    repo = _init_repo(tmp_path)
    subprocess.run(["git", "config", "--local", "core.commentChar", "%"], cwd=tmp_path, check=True)

    assert repo.ensure_comment_char_preserves_headings() is False

    kept = subprocess.run(
        ["git", "config", "--local", "--get", "core.commentChar"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert kept == "%"  # the user's choice stands (and it already protects "#" headings)


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


def test_discover_reads_a_non_ascii_repo_path(tmp_path):
    """aGiTrack was completely unusable on any repo whose path contains a non-ASCII character.

    `GitRepo.discover()` ran git with a bare `text=True`, so git's UTF-8 output was decoded with
    the platform locale — cp1252 on a Western Windows box, ASCII under `PYTHONUTF8=0` on Linux.
    The mojibake path was then handed to the NEXT git call as `cwd`, which failed with
    `[WinError 267] The directory name is invalid` (or raised UnicodeDecodeError outright). Every
    user whose profile directory is `C:\\Users\\Müller` — or any CJK/Cyrillic/Greek username — hit
    this on their first command."""
    nested = tmp_path / "ünïcode dir" / "repo"
    nested.mkdir(parents=True)
    repo = _init_repo(nested)

    assert repo.repo == nested.resolve()
    # ...and the handle keeps working: the discovered path is used as `cwd` for every later call.
    assert repo.current_branch()


def test_discover_names_the_path_instead_of_leaking_an_oserror(tmp_path):
    """`--repo <missing>` and `--repo <a file>` both surfaced as raw OSErrors — a
    `PosixPath('…')` repr on POSIX, a bare `[WinError 267]` with no path at all on Windows, and
    no way to tell the two cases apart. The good sibling message already existed for the
    not-a-repo case."""
    from agitrack.git import GitError
    import pytest

    with pytest.raises(GitError) as missing:
        GitRepo.discover(tmp_path / "nope")
    assert "no such directory" in str(missing.value) and "nope" in str(missing.value)

    a_file = tmp_path / "a-file.txt"
    a_file.write_text("x")
    with pytest.raises(GitError) as not_a_dir:
        GitRepo.discover(a_file)
    assert "not a directory" in str(not_a_dir.value)


def test_utf8_text_is_used_everywhere_git_output_is_decoded():
    """Belt-and-braces guard for the whole class of defect: a bare `text=True` anywhere in the
    package decodes with the platform locale, which is never what aGiTrack wants."""
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path("agitrack").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = getattr(node.func, "attr", None)
            if target not in {"run", "Popen", "check_output"}:
                continue
            names = {kw.arg for kw in node.keywords}
            if "text" in names and "encoding" not in names and None not in names:
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"use **UTF8_TEXT (agitrack/proc.py) instead of text=True: {offenders}"


def test_comment_char_is_explicit_not_the_deprecated_auto(tmp_path):
    """git 2.54 deprecates `core.commentChar=auto`: every subsequent commit prints a deprecation
    warning plus 8-9 hint lines, FOREVER — including long after the user has removed aGiTrack —
    and the hint tells them to unset the very setting that protects aGiTrack's `#` headings. It
    breaks outright in Git 3.0. Six independent live-test scenarios found it."""
    repo = _init_repo(tmp_path)

    assert repo.ensure_comment_char_preserves_headings() is True

    value = subprocess.run(
        ["git", "config", "--local", "--get", "core.commentChar"],
        cwd=repo.repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert value and value != "auto" and value != "#"


def test_comment_char_is_restored_on_teardown(tmp_path):
    """It was written into every tracked repo's local config and never unset — not by `-b stop`,
    not by `--remove-hooks` — so it outlived a full uninstall."""
    repo = _init_repo(tmp_path)
    repo.ensure_comment_char_preserves_headings()

    assert repo.restore_comment_char() is True

    got = subprocess.run(
        ["git", "config", "--local", "--get", "core.commentChar"],
        cwd=repo.repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert got.returncode != 0  # unset
    assert repo.restore_comment_char() is False  # idempotent


def test_a_comment_char_the_user_chose_is_never_touched(tmp_path):
    repo = _init_repo(tmp_path)
    subprocess.run(["git", "config", "--local", "core.commentChar", "%"], cwd=repo.repo, check=True)

    assert repo.ensure_comment_char_preserves_headings() is False
    assert repo.restore_comment_char() is False

    value = subprocess.run(
        ["git", "config", "--local", "--get", "core.commentChar"],
        cwd=repo.repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert value == "%"


def test_a_previously_written_auto_is_migrated(tmp_path):
    """Repos aGiTrack already touched carry the deprecated value; upgrading must fix them."""
    repo = _init_repo(tmp_path)
    subprocess.run(["git", "config", "--local", "core.commentChar", "auto"], cwd=repo.repo, check=True)
    subprocess.run(["git", "config", "--local", "agitrack.commentchar", "auto"], cwd=repo.repo, check=True)

    repo.ensure_comment_char_preserves_headings()

    value = subprocess.run(
        ["git", "config", "--local", "--get", "core.commentChar"],
        cwd=repo.repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert value != "auto"


def test_a_filtered_fetch_leaves_the_repos_config_exactly_as_it_found_it(tmp_path):
    """B12: `--backtrace text`, a documented READ-ONLY report, permanently mutated git config.
    A `--filter` fetch does not just set `partialclonefilter` — git also writes
    `remote.<r>.promisor=true` and bumps `core.repositoryformatversion` to 1, WITHOUT the
    matching `extensions.partialClone`, leaving a state git itself would not produce. Only the
    filter was ever undone. Reproduced from pristine; in a submodule setup it edited the
    vendored dependency's own config."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    repo = _init_repo(work)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=repo.repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], cwd=repo.repo, check=True)

    def local_config() -> dict:
        raw = subprocess.run(
            ["git", "config", "--local", "--list"], cwd=repo.repo, capture_output=True, text=True, check=True
        ).stdout
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    before = local_config()
    assert repo.fetch_ref("+refs/heads/main:refs/agitrack/probe", filter_blobs="blob:limit=16k") is True

    after = local_config()
    for key in ("remote.origin.partialclonefilter", "remote.origin.promisor"):
        assert key not in after, f"{key} was left behind"
    assert after.get("core.repositoryformatversion") == before.get("core.repositoryformatversion")
    # ...and the fetch still did its job.
    assert repo.rev_parse("refs/agitrack/probe")


def test_a_repo_that_really_is_a_partial_clone_keeps_its_settings(tmp_path):
    """The restore must put values BACK, not blanket-unset them: a user whose repo genuinely is
    a partial clone must not have their setup dismantled by a session listing."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    repo = _init_repo(work)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=repo.repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], cwd=repo.repo, check=True)
    subprocess.run(
        ["git", "config", "--local", "remote.origin.partialclonefilter", "blob:none"], cwd=repo.repo, check=True
    )
    subprocess.run(["git", "config", "--local", "remote.origin.promisor", "true"], cwd=repo.repo, check=True)

    repo.fetch_ref("+refs/heads/main:refs/agitrack/probe", filter_blobs="blob:limit=16k")

    kept = subprocess.run(
        ["git", "config", "--local", "--get", "remote.origin.partialclonefilter"],
        cwd=repo.repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert kept == "blob:none"  # the user's own value, restored
