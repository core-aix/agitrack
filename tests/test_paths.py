"""Path SHAPE handling (`agitrack/paths.py`), and the two places that depend on it.

aGiTrack reads paths it did not write: a coding agent's transcript records whatever the
machine it ran on used, and a shared session is read on a different machine entirely. So
these questions are answered from the string, and every case here runs on EVERY platform —
that is the whole point. Windows-shaped input used to answer "not absolute" and "not under
that" on Linux and macOS, and the consequences (an unusable reconstruction, a resumed
session still editing its old worktree) only showed up in a Windows CI job whose failures
were being swallowed.
"""

from __future__ import annotations

import os

from agitrack import paths
from agitrack.metrics import backtrace as bt
from agitrack.transcripts.claude import _rewrite_path_prefixes

WIN_REPO = r"C:\Users\dev\Code\myproject"
POSIX_REPO = "/Users/dev/Code/myproject"


# --- what counts as absolute -------------------------------------------------------


def test_absolute_means_absolute_in_either_shape():
    for absolute in ("/usr/lib/x.py", "~/notes.md", r"C:\Users\dev\x.py", "C:/Users/dev/x.py", r"\\server\share\x"):
        assert paths.is_absolute(absolute), absolute
    for relative in ("pkg/mod.py", r"pkg\mod.py", "x.py", "", "C:", "..\\up.py"):
        assert not paths.is_absolute(relative), relative


# --- being under a directory -------------------------------------------------------


def test_a_windows_path_is_under_its_windows_parent():
    assert paths.under(WIN_REPO + r"\pkg\mod.py", WIN_REPO)
    assert paths.under(WIN_REPO, WIN_REPO)  # the directory itself
    assert not paths.under(r"C:\Users\dev\Code\other\mod.py", WIN_REPO)
    # A separator is not a substring match: "myproject2" is not inside "myproject".
    assert not paths.under(r"C:\Users\dev\Code\myproject2\mod.py", WIN_REPO)


def test_mixed_separators_still_match():
    # Real transcripts contain both in ONE path: the tool joined with "/" onto a Windows cwd.
    assert paths.under(WIN_REPO + "/pkg/mod.py", WIN_REPO)
    assert paths.relative_to(WIN_REPO + r"\pkg/mod.py", WIN_REPO) == "pkg/mod.py"


def test_relative_to_returns_forward_slashes_and_none_when_outside():
    assert paths.relative_to(WIN_REPO + r"\pkg\mod.py", WIN_REPO) == "pkg/mod.py"
    assert paths.relative_to(POSIX_REPO + "/pkg/mod.py", POSIX_REPO) == "pkg/mod.py"
    assert paths.relative_to(WIN_REPO, WIN_REPO) == ""  # the base itself
    assert paths.relative_to("/somewhere/else.py", POSIX_REPO) is None


def test_contains_segments_reads_either_separator():
    assert paths.contains_segments(WIN_REPO + r"\.agitrack\worktrees\feature", "/.agitrack/worktrees/")
    assert paths.contains_segments(POSIX_REPO + "/.agitrack/worktrees/feature", "/.agitrack/worktrees/")
    assert not paths.contains_segments(WIN_REPO + r"\src\feature", "/.agitrack/worktrees/")


def test_windows_comparisons_ignore_case_and_posix_ones_do_not():
    """``C:\\Repo`` and ``c:\\repo`` are one directory on Windows and two on Linux, and a
    transcript may disagree with the caller about which spelling it was."""
    same_but_for_case = paths.under(r"c:\users\dev\Code\myproject\mod.py", WIN_REPO)
    assert same_but_for_case is (os.name == "nt")


# --- the reconstruction's display paths --------------------------------------------


def test_an_edit_recorded_on_windows_becomes_repo_relative():
    """The bug this file exists for: `_display_path` asked only whether a path started with
    "/" or "~", so every Windows path answered "already relative" and was passed through
    whole. The reconstruction then showed `diff --git a/C:\\Users\\...\\hello.py`, and worse,
    `backtrace commit` could not match ANY edit to a commit's files - it reported "no
    untracked AI work to annotate" for a repo full of it."""
    bases = bt._relativize_bases(_FakePath(WIN_REPO), "")
    assert bt._display_path(WIN_REPO + r"\hello.py", bases) == "hello.py"
    assert bt._display_path(WIN_REPO + r"\pkg\mod.py", bases) == "pkg/mod.py"
    # Work done in an aGiTrack worktree collapses onto the file the repo knows.
    assert bt._display_path(WIN_REPO + r"\.agitrack\worktrees\s1\pkg\mod.py", bases) == "pkg/mod.py"
    # ...and something genuinely outside the repo is still dropped, not counted as its work.
    assert bt._display_path(r"C:\Users\dev\scratch\note.txt", bases) is None


def test_a_windows_edit_carries_its_relative_path_into_the_patch_headers():
    edit = _FakeEdit(WIN_REPO + r"\hello.py", "diff --git a/" + WIN_REPO + r"\hello.py b/x" + "\n+print()\n")
    bases = bt._relativize_bases(_FakePath(WIN_REPO), "")
    out = bt._relativize(edit, bases)
    assert out is not None and out.path == "hello.py"
    assert out.patch.startswith("diff --git a/hello.py")


# --- repointing a resumed session --------------------------------------------------


def test_a_resumed_windows_session_is_repointed_out_of_its_worktree():
    """A session first run in a worktree keeps every recorded file path pointing there.
    Matching those by raw string prefix left them all in the worktree on Windows, so the
    resumed agent went on editing a directory aGiTrack had moved it out of."""
    worktree = WIN_REPO + r"\.agitrack\worktrees\feature"
    value = {
        "cwd": worktree,
        "content": [
            {"input": {"file_path": worktree + r"\app.py"}},
            {"input": {"file_path": worktree + "/sub/b.py"}},  # mixed separators, as recorded
            {"input": {"file_path": r"C:\elsewhere\untouched.py"}},
        ],
    }
    out = _rewrite_path_prefixes(value, (worktree,), WIN_REPO)
    assert out["cwd"] == WIN_REPO
    assert out["content"][0]["input"]["file_path"] == WIN_REPO + "/app.py"
    assert out["content"][1]["input"]["file_path"] == WIN_REPO + "/sub/b.py"
    assert out["content"][2]["input"]["file_path"] == r"C:\elsewhere\untouched.py"  # not ours


# --- helpers ------------------------------------------------------------------------


class _FakePath:
    """A stand-in for the reconstruction's directory: `_relativize_bases` only ever str()s it,
    and a real WindowsPath cannot be built on POSIX."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


class _FakeEdit:
    def __init__(self, path: str, patch: str) -> None:
        self.path, self.patch = path, patch
        self.insertions, self.deletions = 1, 0
