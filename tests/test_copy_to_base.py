"""Copying worktree leftovers into the base directory — against REAL git repos.

A worktree session commits its tracked work onto a turn branch that integrates into
the base, but files the agent left *uncommitted* (declined untracked files, unstaged
edits) or that are *git-ignored* (build output, local data) live only in the worktree
and never reach the base directory — where the user actually works. ``ProxyRunner.
_offer_copy_unstaged_to_base`` offers to copy them across.

The unit tests in ``test_proxy.py`` drive this with a mocked ``git status``; these
exercise the same code against a real ``git status --short --ignored`` over a real
worktree, so the porcelain parsing (untracked ``??``, ignored ``!!`` files, and a
wholly-ignored ``dir/``) is pinned to git's actual output.
"""

from __future__ import annotations

import types

from agitrack.git import GitRepo
from agitrack.git.worktree import WorktreeManager

from proxy_helpers import make_runner

# Runs on EVERY OS, including the Windows CI job: this is platform-agnostic flow logic over real
# git worktrees, and Windows is exactly where the copy/commit flow bugs surfaced. (It used to be
# gated POSIX-only on the stale assumption that aGiTrack only ran under WSL; native Windows is
# supported since #118.)


def _session_with_worktree(tmp_path):
    """A real base repo + a real aGiTrack worktree, wired into a test runner so
    ``_offer_copy_unstaged_to_base`` runs end to end."""
    base = GitRepo.init(tmp_path / "base")
    (tmp_path / "base" / "README.md").write_text("hi\n", encoding="utf-8")
    base.stage_paths(["README.md"])
    base.commit("seed")
    manager = WorktreeManager(base)
    info = manager.create("sess", base=base.current_branch())
    wt = GitRepo(info.path)

    runner = make_runner()
    runner.base_repo = types.SimpleNamespace(repo=base.repo)
    runner.repo = wt
    runner.worktree = types.SimpleNamespace(name="sess", path=info.path)
    runner._offer_user_commit_for_worktree_edits = lambda: None  # tested separately
    messages: list[str] = []
    runner._set_message = lambda m, **k: messages.append(m)
    runner._render = lambda *a, **k: None
    return runner, base.repo, info.path, messages


def test_copies_untracked_and_ignored_skips_hidden_and_scaffolding(tmp_path):
    runner, base_dir, wt_dir, _ = _session_with_worktree(tmp_path)
    (wt_dir / ".gitignore").write_text("ignored.txt\nbuilddir/\n", encoding="utf-8")
    (wt_dir / "result.txt").write_text("KEEP-ME\n", encoding="utf-8")  # untracked → offered
    (wt_dir / "ignored.txt").write_text("DATA\n", encoding="utf-8")  # ignored → offered
    (wt_dir / "builddir").mkdir()
    (wt_dir / "builddir" / "out.bin").write_text("BIN\n", encoding="utf-8")  # ignored dir → offered
    (wt_dir / "_scratch.tmp").write_text("no\n", encoding="utf-8")  # `_` → skipped
    (wt_dir / ".env").write_text("SECRET\n", encoding="utf-8")  # `.` → skipped

    # Real git decides what's untracked vs ignored; only the offered set should appear.
    assert sorted(runner._uncommitted_worktree_files()) == ["builddir/", "ignored.txt", "result.txt"]

    runner._select_popup = lambda title, opts, **k: next(o for o in opts if o.startswith("Yes"))
    runner._offer_copy_unstaged_to_base()

    assert (base_dir / "result.txt").read_text(encoding="utf-8") == "KEEP-ME\n"
    assert (base_dir / "ignored.txt").read_text(encoding="utf-8") == "DATA\n"
    assert (base_dir / "builddir" / "out.bin").read_text(encoding="utf-8") == "BIN\n"  # dir copied recursively
    assert not (base_dir / "_scratch.tmp").exists()  # `_`-prefixed never offered
    assert not (base_dir / ".env").exists()  # `.`-prefixed never offered


def test_only_hidden_or_scaffolding_changed_prompts_nothing(tmp_path):
    runner, _base_dir, wt_dir, _ = _session_with_worktree(tmp_path)
    (wt_dir / "__pycache__").mkdir()
    (wt_dir / "__pycache__" / "m.pyc").write_text("x\n", encoding="utf-8")
    (wt_dir / ".env").write_text("SECRET\n", encoding="utf-8")
    (wt_dir / "_tmp").write_text("y\n", encoding="utf-8")

    assert runner._uncommitted_worktree_files() == []
    prompted: list = []
    runner._select_popup = lambda *a, **k: prompted.append(a) or None
    runner._offer_copy_unstaged_to_base()
    assert prompted == []  # nothing offered, so no popup at all


def test_decline_notice_gives_worktree_path(tmp_path):
    runner, base_dir, wt_dir, messages = _session_with_worktree(tmp_path)
    (wt_dir / "keep.txt").write_text("x\n", encoding="utf-8")
    runner._select_popup = lambda title, opts, **k: "No, leave them in the worktree"

    runner._offer_copy_unstaged_to_base()

    assert not (base_dir / "keep.txt").exists()  # left where it was
    notice = messages[-1]
    assert str(wt_dir) in notice  # the worktree path is spelled out for the user
    assert "kept across runs" in notice  # worktrees persist, so the files stay available


def test_overwrite_is_confirmed_before_replacing_base_files(tmp_path):
    runner, base_dir, wt_dir, _ = _session_with_worktree(tmp_path)
    (wt_dir / "dup.txt").write_text("NEW\n", encoding="utf-8")
    (base_dir / "dup.txt").write_text("OLD\n", encoding="utf-8")
    # Consent to copy, then decline the (single) overwrite confirmation → base version survives.
    answers = iter(["Yes, copy to the base repo", "No, keep the base versions"])
    runner._select_popup = lambda title, opts, **k: next(answers)

    runner._offer_copy_unstaged_to_base()

    assert (base_dir / "dup.txt").read_text(encoding="utf-8") == "OLD\n"


def test_unchanged_declined_file_is_not_reprompted(tmp_path):
    runner, _base_dir, wt_dir, _ = _session_with_worktree(tmp_path)
    (wt_dir / "again.txt").write_text("y\n", encoding="utf-8")
    runner._select_popup = lambda title, opts, **k: "No, leave them in the worktree"
    runner._offer_copy_unstaged_to_base()

    seen: list = []
    runner._select_popup = lambda title, opts, **k: seen.append(title) or None
    runner._offer_copy_unstaged_to_base()  # same file, unchanged
    assert seen == []  # fingerprint dedup: not offered again until it changes


_COPY_Q = ("Copy them into the base repo", "won't be merged")
_OVERWRITE_Q = "already exist in the base repo. Overwrite them?"


def test_copy_decision_is_remembered_until_the_set_changes(tmp_path):
    runner, base_dir, wt_dir, _ = _session_with_worktree(tmp_path)
    (wt_dir / "keep.txt").write_text("v1\n", encoding="utf-8")
    titles: list[str] = []
    runner._select_popup = lambda t, o, **k: titles.append(t) or next(x for x in o if x.startswith("Yes"))

    runner._offer_copy_unstaged_to_base()
    assert (base_dir / "keep.txt").read_text(encoding="utf-8") == "v1\n"
    assert runner._copy_always is True
    assert sum(any(m in t for m in _COPY_Q) for t in titles) == 1  # asked once

    # Same set, changed content → the copy decision is kept, so the COPY question is NOT
    # asked again; it auto-copies. (The base now has the file, so an overwrite question may
    # appear — that is a separate decision — but never the copy question.)
    (wt_dir / "keep.txt").write_text("v2\n", encoding="utf-8")
    titles.clear()
    runner._offer_copy_unstaged_to_base()
    assert not any(any(m in t for m in _COPY_Q) for t in titles)
    assert (base_dir / "keep.txt").read_text(encoding="utf-8") == "v2\n"


def test_copy_decision_resets_when_the_file_set_changes(tmp_path):
    runner, base_dir, wt_dir, _ = _session_with_worktree(tmp_path)
    (wt_dir / "a.txt").write_text("a\n", encoding="utf-8")
    runner._select_popup = lambda t, o, **k: next(x for x in o if x.startswith("Yes"))
    runner._offer_copy_unstaged_to_base()
    assert runner._copy_always is True

    # A genuinely new path changes the set → the remembered decision resets and the copy
    # question is asked again.
    (wt_dir / "b.txt").write_text("b\n", encoding="utf-8")
    titles: list[str] = []
    runner._select_popup = lambda t, o, **k: titles.append(t) or next(x for x in o if x.startswith("Yes"))
    runner._offer_copy_unstaged_to_base()
    assert any(any(m in t for m in _COPY_Q) for t in titles)


def test_always_overwrite_skips_later_overwrite_prompts(tmp_path):
    runner, base_dir, wt_dir, _ = _session_with_worktree(tmp_path)
    (wt_dir / "dup.txt").write_text("NEW1\n", encoding="utf-8")
    (base_dir / "dup.txt").write_text("OLD\n", encoding="utf-8")  # a conflict from the start
    answers = iter(["Yes, copy to the base repo", "Yes, always overwrite (don't ask again)"])
    runner._select_popup = lambda t, o, **k: next(answers)

    runner._offer_copy_unstaged_to_base()
    assert (base_dir / "dup.txt").read_text(encoding="utf-8") == "NEW1\n"
    assert runner._overwrite_always is True and runner._copy_always is True

    # Same set, changed content → neither the copy NOR the overwrite question is asked again.
    (wt_dir / "dup.txt").write_text("NEW2\n", encoding="utf-8")
    titles: list[str] = []
    runner._select_popup = lambda t, o, **k: titles.append(t) or "No, keep the base versions"
    runner._offer_copy_unstaged_to_base()
    assert titles == []  # fully automatic now
    assert (base_dir / "dup.txt").read_text(encoding="utf-8") == "NEW2\n"
