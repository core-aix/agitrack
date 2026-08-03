"""The base-commit guard, against REAL git — including the `--no-verify` bypass.

In worktree mode the agent works in a linked worktree and aGiTrack commits and merges for it.
An agent that instead commits into the BASE repo puts an untracked commit on the user's branch:
there is no fold hook there, and `_uncovered_backend_commits` only scans the session's managed
turn branch, so that commit is invisible to aGiTrack forever. The guard exists to stop it.

`git commit --no-verify` skips `pre-commit` — git's documented behaviour, which no hook can
change — so the pre-commit guard alone is advisory. `reference-transaction` is NOT skipped and
aborts the ref update itself, so the commit cannot land. Both are installed.

That second hook fires on EVERY ref update, so most of this file is about what it must NOT
break: fetch, checkout, tag, branch creation, and the agent's own work inside its worktree.
Everything runs against real git — a guard that only works in theory is worse than none, because
it is trusted.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from agitrack.git import hooks as git_hooks

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh hooks")


def _git(cwd, *args, guard=False, check=True):
    env = {**os.environ}
    if guard:
        env[git_hooks.ENV_GUARD] = "1"  # the marker aGiTrack sets on the AGENT's process only
    else:
        env.pop(git_hooks.ENV_GUARD, None)
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check, encoding="utf-8"
    )


@pytest.fixture
def guarded_repo(tmp_path):
    """A real repo with the guard installed, plus a linked worktree (the agent's sandbox)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    assert git_hooks.install_base_commit_guard(repo / ".git" / "hooks")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "agitrack/session")
    return repo, worktree


def _reference_transaction_supported(repo) -> bool:
    # The hook landed in git 2.28. Older git simply never calls it, which is why pre-commit is
    # kept as the always-available first line.
    version = _git(repo, "--version").stdout.split()[2]
    parts = [int(p) for p in version.split(".")[:2] if p.isdigit()]
    return parts >= [2, 28]


# --- the guard blocks a base-repo commit ------------------------------------


def test_the_agent_cannot_commit_into_the_base_repo(guarded_repo):
    repo, _worktree = guarded_repo
    (repo / "f.txt").write_text("agent edit in the base repo\n")

    result = _git(repo, "commit", "-aqm", "agent commit", guard=True, check=False)

    assert result.returncode != 0
    assert "commit inside your worktree" in result.stderr
    assert _git(repo, "log", "--oneline").stdout.count("\n") == 1  # nothing landed


def test_no_verify_cannot_get_a_commit_past_the_guard(guarded_repo):
    """The whole reason the second hook exists.

    Before it, this succeeded: the commit landed on the user's base branch with no aGiTrack
    metadata and nothing that would ever reconcile it.
    """
    repo, _worktree = guarded_repo
    if not _reference_transaction_supported(repo):
        pytest.skip("git < 2.28 has no reference-transaction hook")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("agent edit, bypassing the hook\n")

    result = _git(repo, "commit", "-aqm", "sneaky", "--no-verify", guard=True, check=False)

    assert result.returncode != 0
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before, "the commit landed anyway"


def test_the_user_is_never_blocked(guarded_repo):
    # The guard keys on a marker aGiTrack sets only on the AGENT's process. The user commits in
    # their own repo constantly and must never see this.
    repo, _worktree = guarded_repo
    (repo / "f.txt").write_text("the user's own edit\n")

    _git(repo, "commit", "-aqm", "user commit", guard=False)

    assert "user commit" in _git(repo, "log", "--oneline").stdout


def test_the_agent_commits_freely_inside_its_worktree(guarded_repo):
    # The guard must block the base repo and NOTHING else — the worktree is where the agent is
    # supposed to work, and both hooks are shared across all linked worktrees.
    repo, worktree = guarded_repo
    (worktree / "f.txt").write_text("agent edit in its own worktree\n")

    _git(worktree, "commit", "-aqm", "agent worktree commit", guard=True)

    assert "agent worktree commit" in _git(worktree, "log", "--oneline").stdout


def test_the_agent_commits_freely_in_its_worktree_even_with_no_verify(guarded_repo):
    repo, worktree = guarded_repo
    (worktree / "f.txt").write_text("more agent work\n")

    _git(worktree, "commit", "-aqm", "agent worktree commit", "--no-verify", guard=True)

    assert "agent worktree commit" in _git(worktree, "log", "--oneline").stdout


# --- what the reference-transaction hook must NOT break ---------------------
#
# It fires on every ref update. Over-reaching would break the agent's git entirely rather than
# just blocking a commit, so each ordinary operation is pinned explicitly.


def test_checkout_still_works_under_the_guard(guarded_repo):
    repo, _worktree = guarded_repo
    _git(repo, "branch", "other")

    _git(repo, "checkout", "-q", "other", guard=True)  # moves HEAD only

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "other"


def test_creating_a_branch_still_works_under_the_guard(guarded_repo):
    # A creation moves no history (old value is all-zeros), so it is deliberately allowed.
    repo, _worktree = guarded_repo

    _git(repo, "branch", "feature", guard=True)

    assert "feature" in _git(repo, "branch").stdout


def test_tagging_still_works_under_the_guard(guarded_repo):
    repo, _worktree = guarded_repo

    _git(repo, "tag", "v1", guard=True)

    assert "v1" in _git(repo, "tag").stdout


def test_fetching_still_works_under_the_guard(tmp_path, guarded_repo):
    # Fetch updates refs/remotes/*, never refs/heads/*. Blocking it would leave the agent unable
    # to see the remote at all.
    repo, _worktree = guarded_repo
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main", guard=False)

    result = _git(repo, "fetch", "-q", "origin", guard=True, check=False)

    assert result.returncode == 0, result.stderr


def test_reading_history_is_never_affected(guarded_repo):
    # Reads create no ref transaction at all; pinned so nobody "tightens" the hook into one.
    repo, _worktree = guarded_repo

    assert _git(repo, "log", "--oneline", guard=True).returncode == 0
    assert _git(repo, "status", "--short", guard=True).returncode == 0


# --- installation, chaining and removal -------------------------------------


def test_both_hooks_are_installed_and_removed_together(tmp_path):
    hooks_dir = tmp_path / "hooks"

    assert git_hooks.install_base_commit_guard(hooks_dir)
    assert (hooks_dir / "pre-commit").exists()
    assert (hooks_dir / "reference-transaction").exists()

    git_hooks.remove_base_commit_guard(hooks_dir)

    assert not (hooks_dir / "pre-commit").exists()
    assert not (hooks_dir / "reference-transaction").exists()


def test_an_existing_project_reference_transaction_hook_is_chained_not_destroyed(tmp_path):
    # Losing a project's own hook would be a far worse bug than the one the guard prevents.
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    original = hooks_dir / "reference-transaction"
    original.write_text("#!/bin/sh\necho project hook ran\n")
    original.chmod(0o755)

    git_hooks.install_base_commit_guard(hooks_dir)
    assert (hooks_dir / "reference-transaction.agitrack-orig").exists()

    git_hooks.remove_base_commit_guard(hooks_dir)
    assert original.read_text() == "#!/bin/sh\necho project hook ran\n"


def test_a_chained_project_hook_still_runs_and_receives_its_stdin(guarded_repo, tmp_path):
    """The guard consumes stdin to inspect the refs, so it must replay it downstream — a
    project hook that reads its refs would otherwise see nothing and silently misbehave."""
    repo, _worktree = guarded_repo
    hooks_dir = repo / ".git" / "hooks"
    git_hooks.remove_base_commit_guard(hooks_dir)
    log = tmp_path / "chained.log"
    original = hooks_dir / "reference-transaction"
    original.write_text(f'#!/bin/sh\ncat >> "{log}"\nexit 0\n')
    original.chmod(0o755)
    git_hooks.install_base_commit_guard(hooks_dir)

    (repo / "f.txt").write_text("a user commit\n")
    _git(repo, "commit", "-aqm", "user commit", guard=False)

    assert log.exists(), "the chained project hook never ran"
    assert "refs/heads/main" in log.read_text(), "the chained hook did not receive its stdin"


def test_a_chained_hook_can_still_veto(guarded_repo, tmp_path):
    # The guard must not swallow a downstream refusal.
    repo, _worktree = guarded_repo
    hooks_dir = repo / ".git" / "hooks"
    git_hooks.remove_base_commit_guard(hooks_dir)
    original = hooks_dir / "reference-transaction"
    original.write_text('#!/bin/sh\ncat > /dev/null\nif [ "$1" = "prepared" ]; then exit 1; fi\nexit 0\n')
    original.chmod(0o755)
    git_hooks.install_base_commit_guard(hooks_dir)

    (repo / "f.txt").write_text("blocked by the project's own hook\n")
    result = _git(repo, "commit", "-aqm", "nope", guard=False, check=False)

    assert result.returncode != 0


def test_installing_twice_does_not_clobber_the_backup(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "reference-transaction").write_text("#!/bin/sh\necho original\n")
    (hooks_dir / "reference-transaction").chmod(0o755)

    git_hooks.install_base_commit_guard(hooks_dir)
    git_hooks.install_base_commit_guard(hooks_dir)

    assert "original" in (hooks_dir / "reference-transaction.agitrack-orig").read_text()


def test_removal_leaves_a_foreign_hook_untouched(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    foreign = hooks_dir / "reference-transaction"
    foreign.write_text("#!/bin/sh\necho not ours\n")

    git_hooks.remove_base_commit_guard(hooks_dir)

    assert foreign.exists() and "not ours" in foreign.read_text()


def test_the_guard_never_names_gits_bypass_flag(tmp_path):
    # The refusal is only ever shown to the AGENT. Naming the flag would hand it the way around
    # the advisory hook. (There is no way around the reference-transaction one.)
    hooks_dir = tmp_path / "hooks"
    git_hooks.install_base_commit_guard(hooks_dir)

    for name in ("pre-commit", "reference-transaction"):
        assert "--no-verify" not in (hooks_dir / name).read_text()


def test_agitracks_own_integration_is_never_blocked(guarded_repo):
    """The highest-consequence failure mode of this guard.

    aGiTrack merges each turn's worktree branch into the base branch FROM ITS OWN PROCESS, which
    updates `refs/heads/<base>` — exactly the transaction the reference-transaction guard
    aborts. It works only because the marker is set on the AGENT CHILD's environment and
    nowhere else (`_backend_child_env`). If that ever changed, aGiTrack could not merge at all
    and every worktree session would silently stop integrating.
    """
    repo, worktree = guarded_repo
    (worktree / "f.txt").write_text("the agent's committed work\n")
    _git(worktree, "commit", "-aqm", "agent turn", guard=True)  # allowed: inside the worktree

    # aGiTrack's own merge into the base branch, without the agent's marker.
    result = _git(repo, "merge", "-q", "--no-ff", "-m", "integrate", "agitrack/session", guard=False, check=False)

    assert result.returncode == 0, f"aGiTrack's own integration was blocked: {result.stderr}"
    assert "the agent's committed work" in (repo / "f.txt").read_text()


def test_the_marker_is_only_ever_set_on_the_agent_child():
    # Stated at the source, so the guard's whole safety argument is checked rather than assumed.
    import inspect

    from agitrack.proxy import runner as runner_module

    source = inspect.getsource(runner_module.ProxyRunner._backend_child_env)
    assert "ENV_GUARD" in source, "the marker must be set in the agent child's env"
    others = [
        line
        for line in inspect.getsource(runner_module).splitlines()
        if "ENV_GUARD] = " in line or "ENV_GUARD]=" in line
    ]
    assert len(others) == 1, f"the guard marker is set in more than one place: {others}"
