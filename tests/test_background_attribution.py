"""Background-task file attribution (agitrack/commits/actions.py).

A task the agent backgrounded keeps writing files after its turn was committed — most
visibly under --no-worktree, where it writes the very tree the user works in. These
tests lock in the attribution that stops aGiTrack's automatic "commit your changes?"
dialog from claiming a background job's output as the user's own edits.
"""

from __future__ import annotations

import subprocess

from agitrack.commits import actions as actions_mod
from agitrack.commits.actions import (
    AgitrackActions,
    background_authored_sets,
    is_background_authored,
    split_background_paths,
    unstage_background_authored,
)
from agitrack.config import AgitrackState
from agitrack.git import GitRepo


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "README.md").write_text("# demo\n")
    results = path / "results" / "run-1"
    results.mkdir(parents=True)
    (results / "state.json").write_text('{"step": 0}\n')
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _background_commit(path, step):
    # An agent commit whose recorded prompts are ONLY background labels: the shape the
    # attribution scans for (a monitor tick / completed background task turn).
    (path / "results" / "run-1" / "state.json").write_text('{"step": %d}\n' % step)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    message = (
        "<aGiTrack> experiment tick\n\n# Interaction Trace\n\n## User\n\n"
        "(background monitor update)\n\n## Agent\n\nNoted.\n\n"
        "# aGiTrack Metadata\ncommit_type: agent\nbackend: claude\n"
    )
    subprocess.run(["git", "commit", "-qm", message], cwd=path, check=True)


def test_background_authored_sets_scans_labelled_commits(tmp_path):
    repo = _init_repo(tmp_path)
    _background_commit(tmp_path, 1)
    actions_mod._background_paths_cache.clear()
    paths, dirs = background_authored_sets(repo)
    assert "results/run-1/state.json" in paths
    assert "results" in dirs and "results/run-1" in dirs
    sets = (paths, dirs)
    # Known file, and NEW files under the background job's tree, attribute to background.
    assert is_background_authored("results/run-1/state.json", sets)
    assert is_background_authored("results/run-2/new.json", sets)
    assert not is_background_authored("README.md", sets)


def test_dialog_not_raised_for_background_only_changes(tmp_path):
    repo = _init_repo(tmp_path)
    _background_commit(tmp_path, 1)
    actions_mod._background_paths_cache.clear()
    actions = AgitrackActions(repo, AgitrackState(tmp_path), interactive=False)
    # The background job ticks again: tracked change + a fresh run directory.
    (tmp_path / "results" / "run-1" / "state.json").write_text('{"step": 2}\n')
    (tmp_path / "results" / "run-2").mkdir()
    (tmp_path / "results" / "run-2" / "state.json").write_text('{"step": 0}\n')
    assert actions.has_pre_agent_user_changes() is False
    # A genuine user edit still raises it.
    (tmp_path / "README.md").write_text("# demo\nedited by hand\n")
    assert actions.has_pre_agent_user_changes() is True


def test_unstage_background_authored_keeps_user_files_staged(tmp_path):
    repo = _init_repo(tmp_path)
    _background_commit(tmp_path, 1)
    actions_mod._background_paths_cache.clear()
    (tmp_path / "results" / "run-1" / "state.json").write_text('{"step": 3}\n')
    (tmp_path / "README.md").write_text("# demo\nedited by hand\n")
    repo.add_tracked()
    skipped = unstage_background_authored(repo)
    assert skipped == ["results/run-1/state.json"]
    assert repo.staged_paths() == ["README.md"]
    # The background file stays modified in the tree for the agent's next commit.
    assert "results/run-1/state.json" in repo.changed_tracked_paths()


def test_split_background_paths_without_history_is_a_noop(tmp_path):
    # A repo with no background-labelled commits keeps today's behaviour untouched.
    repo = _init_repo(tmp_path)
    actions_mod._background_paths_cache.clear()
    user, background = split_background_paths(repo, ["README.md", "results/run-1/state.json"])
    assert user == ["README.md", "results/run-1/state.json"] and background == []
