import subprocess

from agit.git import GitRepo
from agit.shell import AgitShell
from agit.state import AgitState


def test_declined_untracked_files_do_not_count_as_pre_agent_changes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    (tmp_path / "declined.txt").write_text("declined", encoding="utf-8")
    repo = GitRepo.discover(tmp_path)
    state = AgitState(repo.repo)
    state.add_declined(["declined.txt"])

    shell = AgitShell(repo)

    assert shell.actions.has_pre_agent_user_changes() is False


def test_new_promptable_untracked_files_count_as_pre_agent_changes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")

    shell = AgitShell(GitRepo.discover(tmp_path))

    assert shell.actions.has_pre_agent_user_changes() is True
