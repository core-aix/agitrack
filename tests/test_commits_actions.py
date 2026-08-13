"""Tests for agitrack/commits/actions.py.

Covers the branches missed by the existing suite: the verbose no-staged-changes
path, the empty-message retry loop (both terminal and UI), and the terminal
interactive untracked-file review (y/s/default).
"""

from __future__ import annotations

import pytest

from unittest.mock import MagicMock, patch

from agitrack.commits.actions import AgitrackActions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_actions(*, verbose=False, interactive=True, ui=None, has_staged=False, has_changes=False):
    repo = MagicMock()
    repo.has_staged_changes.return_value = has_staged
    repo.has_changes.return_value = has_changes
    repo.untracked_files.return_value = []
    repo.untracked_entries.return_value = []
    state = MagicMock()
    state.declined_untracked.return_value = []
    state.session_id = "sess-123"
    return AgitrackActions(repo, state, verbose=verbose, interactive=interactive, ui=ui), repo, state


# ---------------------------------------------------------------------------
# create_user_commit — no staged changes
# ---------------------------------------------------------------------------


def test_create_user_commit_no_staged_silent(capsys):
    actions, repo, _ = _make_actions(verbose=False)
    result = actions.create_user_commit()
    assert result is False
    captured = capsys.readouterr()
    assert captured.out == ""


def test_create_user_commit_no_staged_verbose(capsys):
    actions, repo, _ = _make_actions(verbose=True)
    result = actions.create_user_commit()
    assert result is False
    assert "No staged" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# create_user_commit — terminal empty-message retry loop
# ---------------------------------------------------------------------------


def test_create_user_commit_terminal_empty_then_valid(capsys):
    actions, repo, state = _make_actions(has_staged=True, interactive=True)
    with patch("builtins.input", side_effect=["", "fix: the bug"]):
        result = actions.create_user_commit()
    assert result is True
    out = capsys.readouterr().out
    assert "required" in out


def test_create_user_commit_terminal_retries_until_non_empty(capsys):
    actions, repo, state = _make_actions(has_staged=True, interactive=True)
    with patch("builtins.input", side_effect=["  ", "", "valid message"]):
        actions.create_user_commit()
    out = capsys.readouterr().out
    assert out.count("required") == 2


# ---------------------------------------------------------------------------
# create_user_commit — UI empty-message retry loop
# ---------------------------------------------------------------------------


def test_create_user_commit_ui_empty_then_valid():
    ui = MagicMock()
    ui.text.side_effect = ["", "fix: the bug"]
    actions, repo, state = _make_actions(has_staged=True, ui=ui)
    result = actions.create_user_commit()
    assert result is True
    # ui.info must have been called with the "required" warning at least once
    calls = [c.args[0] for c in ui.info.call_args_list]
    assert any("required" in msg for msg in calls)


def test_create_user_commit_ui_cancel_returns_false():
    ui = MagicMock()
    ui.text.return_value = None  # user cancelled the dialog
    actions, repo, state = _make_actions(has_staged=True, ui=ui)
    result = actions.create_user_commit()
    assert result is False
    ui.info.assert_called_once()
    # Cancelling continues without committing; the notice says so.
    assert "without committing" in ui.info.call_args.args[0].lower()


# ---------------------------------------------------------------------------
# review_untracked — terminal interactive paths
# ---------------------------------------------------------------------------


def _make_actions_with_untracked(*files, interactive=True):
    repo = MagicMock()
    repo.untracked_files.return_value = list(files)
    repo.untracked_entries.return_value = list(files)  # review/decline flow uses entries
    state = MagicMock()
    state.declined_untracked.return_value = []
    return AgitrackActions(repo, state, interactive=interactive), repo, state


def test_review_untracked_terminal_yes_stages_all():
    actions, repo, state = _make_actions_with_untracked("a.py", "b.py")
    with patch("builtins.input", return_value="y"):
        actions.review_untracked(include_declined=True)
    repo.stage_paths.assert_called_once_with(["a.py", "b.py"])
    state.remove_declined.assert_called_once()


def test_review_untracked_terminal_yes_alias(capsys):
    actions, repo, state = _make_actions_with_untracked("x.py")
    with patch("builtins.input", return_value="yes"):
        actions.review_untracked(include_declined=True)
    repo.stage_paths.assert_called_once()


def test_review_untracked_terminal_select_stages_chosen(capsys):
    actions, repo, state = _make_actions_with_untracked("a.py", "b.py", "c.py")
    # First input is "s" (select), second is "1 3" (items 1 and 3)
    with patch("builtins.input", side_effect=["s", "1 3"]):
        actions.review_untracked(include_declined=True)
    repo.stage_paths.assert_called_once_with(["a.py", "c.py"])
    # b.py was declined
    declined_call = state.add_declined.call_args[0][0]
    assert "b.py" in declined_call


def test_review_untracked_terminal_default_declines_all():
    actions, repo, state = _make_actions_with_untracked("a.py", "b.py")
    with patch("builtins.input", return_value=""):
        actions.review_untracked(include_declined=True)
    repo.stage_paths.assert_not_called()
    state.add_declined.assert_called_once_with(["a.py", "b.py"])


def test_review_untracked_terminal_n_declines_all():
    actions, repo, state = _make_actions_with_untracked("a.py")
    with patch("builtins.input", return_value="n"):
        actions.review_untracked(include_declined=True)
    repo.stage_paths.assert_not_called()
    state.add_declined.assert_called_once()


def test_review_untracked_non_interactive_stages_everything(capsys):
    actions, repo, state = _make_actions_with_untracked("a.py", "b.py", interactive=False)
    actions.review_untracked(include_declined=True)
    repo.stage_paths.assert_called_once_with(["a.py", "b.py"])


# ---------------------------------------------------------------------------
# _select_paths
# ---------------------------------------------------------------------------


def test_select_paths_valid_numbers():
    actions, _, _ = _make_actions_with_untracked("a.py", "b.py", "c.py")
    with patch("builtins.input", return_value="1 3"):
        result = actions._select_paths(["a.py", "b.py", "c.py"])
    assert result == ["a.py", "c.py"]


def test_select_paths_out_of_range_ignored():
    actions, _, _ = _make_actions_with_untracked()
    with patch("builtins.input", return_value="1 99 2"):
        result = actions._select_paths(["a.py", "b.py"])
    assert result == ["a.py", "b.py"]


def test_select_paths_empty_input_returns_empty():
    actions, _, _ = _make_actions_with_untracked()
    with patch("builtins.input", return_value="  "):
        result = actions._select_paths(["a.py", "b.py"])
    assert result == []


def test_select_paths_non_numeric_ignored():
    actions, _, _ = _make_actions_with_untracked()
    with patch("builtins.input", return_value="abc 1"):
        result = actions._select_paths(["a.py", "b.py"])
    assert result == ["a.py"]


def _dirty_repo(tmp_path):
    import subprocess

    from agitrack.git import GitRepo

    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    (root / "a.txt").write_text("one\nuser edit\n", encoding="utf-8")
    return GitRepo.discover(root)


def _actions(repo):
    from agitrack.commits import AgitrackActions
    from agitrack.config import AgitrackState

    return AgitrackActions(repo, AgitrackState(repo.repo), interactive=True)


def test_typing_skip_where_skipping_is_impossible_never_becomes_the_commit_message(tmp_path, monkeypatch):
    """B9: in worktree mode the pre-agent commit is mandatory and `skip` was not recognised, so
    it became the MESSAGE — a commit literally named `skip` landed on main. The same word means
    "back out" one mode over (--no-worktree offers it explicitly), which is exactly why people
    reach for it here."""
    repo = _dirty_repo(tmp_path)
    answers = iter(["skip", "a real message"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))

    assert _actions(repo).create_user_commit(allow_skip=False) is True

    assert repo._run(["git", "log", "-1", "--format=%s"]).stdout.strip() == "a real message"


def test_the_mandatory_prompt_has_a_way_out(tmp_path, monkeypatch):
    """The prompt had NO exit at all: empty Enter re-asked forever and Ctrl-C was swallowed —
    one live run spent 150 s and six Ctrl-C without escaping. A required question still has to
    have an answer that ends it."""
    from agitrack.commits import UserCommitAborted

    repo = _dirty_repo(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _p="": "quit")

    with pytest.raises(UserCommitAborted):
        _actions(repo).create_user_commit(allow_skip=False)

    assert repo.staged_paths() == []  # and it leaves the index exactly as it found it


def test_ctrl_c_at_the_mandatory_prompt_aborts_rather_than_continuing(tmp_path, monkeypatch):
    """Swallowing it meant launching into a worktree that is missing the user's uncommitted
    work — the very thing the prompt exists to prevent."""
    from agitrack.commits import UserCommitAborted

    repo = _dirty_repo(tmp_path)

    def _interrupt(_p=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)

    with pytest.raises(UserCommitAborted):
        _actions(repo).create_user_commit(allow_skip=False)
    assert repo.staged_paths() == []


def test_skip_still_works_where_it_is_offered(tmp_path, monkeypatch):
    """--no-worktree offers skip, and declining must leave the index untouched — staging on the
    user's behalf and leaving it there hides a half-resolved file under "changes to be
    committed"."""
    repo = _dirty_repo(tmp_path)
    (repo.repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    before = repo._run(["git", "status", "--porcelain"]).stdout
    monkeypatch.setattr("builtins.input", lambda _p="": "skip")

    assert _actions(repo).create_user_commit(allow_skip=True) is False

    assert repo._run(["git", "status", "--porcelain"]).stdout == before
    assert repo.staged_paths() == []
