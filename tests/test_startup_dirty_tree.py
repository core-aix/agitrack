"""Starting aGiTrack with a dirty tree, and refusing to start mid-conflict.

Three behaviours, all reported from real use:

* **Declining the startup commit must leave the index alone.** The dialog stages on the user's
  behalf so it can show what a commit would contain. When the user backed out, that staging
  stayed — and during a merge conflict `git status` files staged paths under "changes to be
  committed", so a half-resolved file stopped standing out as one still needing work.
* **Declining has to be possible under ``--no-worktree``.** The console path looped until a
  message was given, with no way out, so a dirty tree made starting impossible. It stays
  required in worktree mode, where the session's worktree is checked out from HEAD and
  uncommitted work would simply not be in the tree the agent reads.
* **An unresolved conflict must stop the start outright.** There is no correct reading of
  "commit this" while a merge is half-done.
"""

from __future__ import annotations

import subprocess

import pytest

from agitrack.cli import _refuse_during_merge_conflict
from agitrack.commits.actions import AgitrackActions
from agitrack.config import AgitrackState
from agitrack.git import GitRepo


def _repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _actions(repo, ui=None, interactive=True):
    return AgitrackActions(repo, AgitrackState(repo.repo, default_backend="claude"), ui=ui, interactive=interactive)


class _UI:
    """A scripted InteractiveUI. ``answers`` are returned from text() in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.infos: list[str] = []
        self.questions: list[str] = []

    def text(self, message, **kw):
        self.questions.append(message)
        return self.answers.pop(0) if self.answers else None

    def info(self, message, *, level="info"):
        self.infos.append(message)

    def select(self, message, options, **kw):
        return None

    def multiselect(self, message, options, **kw):
        return []

    def confirm(self, message):
        return False


# ---------------------------------------------------------------- staging on decline


def test_declining_the_commit_leaves_the_index_untouched(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("edited\n")
    ui = _UI([None])  # Esc at the message prompt

    assert _actions(repo, ui=ui).create_user_commit() is False

    assert repo.staged_paths() == []  # NOT left staged
    assert (tmp_path / "f.txt").read_text() == "edited\n"  # and the edit is still there


def test_declining_preserves_what_the_user_had_already_staged(tmp_path):
    # Only the staging aGiTrack did on their behalf is undone. A file the user staged
    # themselves before starting must survive — unstaging it would be its own data loss.
    repo = _repo(tmp_path)
    (tmp_path / "mine.txt").write_text("staged by hand\n")
    subprocess.run(["git", "add", "mine.txt"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("edited\n")
    ui = _UI([None])

    assert _actions(repo, ui=ui).create_user_commit() is False

    assert repo.staged_paths() == ["mine.txt"]


def test_committing_still_captures_everything(tmp_path):
    # The undo must not touch the path where the user DOES commit.
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("edited\n")
    ui = _UI(["my message"])

    assert _actions(repo, ui=ui).create_user_commit() is True

    assert repo.staged_paths() == []  # committed, so nothing is left staged
    assert (
        "my message"
        in subprocess.run(["git", "log", "-1", "--format=%B"], cwd=tmp_path, capture_output=True, text=True).stdout
    )


# ---------------------------------------------------------------- skippable or not


def test_no_worktree_mode_can_decline_and_carry_on(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("edited\n")
    ui = _UI([None])

    assert _actions(repo, ui=ui).create_user_commit(allow_skip=True) is False

    assert any("without committing" in info for info in ui.infos)


def test_worktree_mode_refuses_to_skip_and_says_why(tmp_path):
    # Esc is not an exit here: the worktree is checked out from HEAD, so declining would hand
    # the agent a tree without the user's work. Re-asks instead.
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("edited\n")
    ui = _UI([None, "committed after being told why"])

    assert _actions(repo, ui=ui).create_user_commit(allow_skip=False) is True

    assert any("Cannot skip" in info for info in ui.infos)
    assert "checked out from HEAD" in ui.questions[0]


def test_the_console_path_has_an_explicit_way_out(tmp_path, monkeypatch, capsys):
    # The reported dead end: no ui, --no-worktree, dirty tree — the loop demanded a message
    # forever and the user could not start at all. The exit is a WORD, not an empty line: a
    # stray Enter must never be read as "leave my work uncommitted" (and two existing tests
    # pin that empty keeps re-prompting).
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("edited\n")
    answers = iter(["", "skip"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert _actions(repo).create_user_commit(allow_skip=True) is False

    out = capsys.readouterr().out
    assert "required" in out  # the empty answer re-prompted...
    assert "Continuing without committing" in out  # ...and 'skip' got out
    assert repo.staged_paths() == []


def test_the_skip_word_is_not_offered_when_a_commit_is_required(tmp_path, monkeypatch):
    # In worktree mode there is no way out, so the prompt must not advertise one — and typing
    # it must be treated as an ordinary (if odd) commit message rather than an escape.
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("edited\n")
    prompts: list[str] = []

    def ask(prompt=""):
        prompts.append(prompt)
        return "skip"

    monkeypatch.setattr("builtins.input", ask)

    assert _actions(repo).create_user_commit(allow_skip=False) is True

    assert all("skip" not in prompt for prompt in prompts)


def test_the_console_path_does_not_spin_when_stdin_is_gone(tmp_path, monkeypatch):
    # EOF used to escape only by propagating; the loop must end cleanly and unstage.
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("edited\n")

    def boom(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)

    assert _actions(repo).create_user_commit(allow_skip=False) is False

    assert repo.staged_paths() == []


# ---------------------------------------------------------------- merge conflicts


def _conflicted(path):
    repo = _repo(path)
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=path, check=True)
    (path / "f.txt").write_text("theirs\n")
    subprocess.run(["git", "commit", "-qam", "theirs"], cwd=path, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=path, check=True)
    (path / "f.txt").write_text("ours\n")
    subprocess.run(["git", "commit", "-qam", "ours"], cwd=path, check=True)
    subprocess.run(["git", "merge", "other"], cwd=path, capture_output=True)
    return repo


def test_a_conflicted_repo_refuses_to_start(tmp_path, capsys):
    repo = _conflicted(tmp_path)
    assert repo.unmerged_paths() == ["f.txt"]

    assert _refuse_during_merge_conflict(repo) is True

    out = capsys.readouterr().out
    assert "can't start" in out
    assert "f.txt" in out  # names the files still needing work
    assert "merge --abort" in out  # and how to get out of it


def test_a_clean_repo_starts_normally(tmp_path):
    assert _refuse_during_merge_conflict(_repo(tmp_path)) is False


def test_a_merely_dirty_repo_still_starts(tmp_path):
    # Uncommitted edits are the ordinary case the startup commit dialog exists for — they must
    # not be mistaken for a conflict.
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("edited\n")
    (tmp_path / "new.txt").write_text("new\n")

    assert _refuse_during_merge_conflict(repo) is False


def test_a_conflict_stopped_by_rebase_is_caught_too(tmp_path):
    # A rebase/cherry-pick conflict writes no MERGE_HEAD, so checking that alone would miss it
    # while leaving exactly the same half-resolved tree.
    repo = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("topic\n")
    subprocess.run(["git", "commit", "-qam", "topic"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("main\n")
    subprocess.run(["git", "commit", "-qam", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "topic"], cwd=tmp_path, check=True)
    subprocess.run(["git", "rebase", "main"], cwd=tmp_path, capture_output=True)

    assert repo.merge_in_progress() is False  # no MERGE_HEAD…
    assert _refuse_during_merge_conflict(repo) is True  # …but still refused


@pytest.mark.parametrize("allow_skip", [True, False])
def test_nothing_to_commit_is_not_treated_as_a_decline_to_undo(tmp_path, allow_skip):
    # A clean tree short-circuits before any prompt; the index restore must be a no-op there.
    repo = _repo(tmp_path)

    assert _actions(repo).create_user_commit(allow_skip=allow_skip) is False

    assert repo.staged_paths() == []
