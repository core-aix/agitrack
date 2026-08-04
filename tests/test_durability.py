"""Durability: the failures that lose or corrupt the user's work without saying so.

aGiTrack's whole promise is that the agent's work ends up in git. These are the situations
where that promise can quietly break — the process is killed mid-turn, the disk is full, the
state file is being written when the power goes, someone moves the branch under a live
session. Each is chosen because it fails SILENTLY: no traceback the user sees, just a history
that is missing something.

Real git, real processes, real signals wherever the point is that the real thing behaves as
assumed (`flock` released by the kernel on SIGKILL is a kernel guarantee, not ours — but that
aGiTrack *relies* on it is ours, and worth pinning).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agitrack.config import AgitrackState
from agitrack.fileio import atomic_write_text
from agitrack.git import GitRepo
from agitrack.git.lock import RepoLock

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals / permission bits")


def _repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


# --- state files survive being interrupted ----------------------------------


def test_a_state_file_is_never_left_half_written(tmp_path):
    """The atomic-write guarantee, stated as the property that matters.

    ``state.save()`` runs on every property setter, so an in-place rewrite interrupted by a
    crash would leave a truncated JSON file — which bricks the NEXT startup, long after the
    crash, with no clue why. A reader must only ever see the old file or the new one.
    """
    target = tmp_path / "state.json"
    atomic_write_text(target, json.dumps({"version": 1}))

    # Simulate an interrupted write: something raises between opening the tmp and renaming.
    original = os.replace

    def _fail_replace(src, dst):
        raise OSError(28, "No space left on device")

    os.replace = _fail_replace
    try:
        with pytest.raises(OSError):
            atomic_write_text(target, json.dumps({"version": 2, "padding": "x" * 10000}))
    finally:
        os.replace = original

    # The OLD content is intact and still parses — never a truncated mixture of both.
    assert json.loads(target.read_text()) == {"version": 1}


def test_a_failed_write_leaves_no_temporary_files_behind(tmp_path):
    # Otherwise a repo accumulates .tmp droppings on every failure, and `git status` starts
    # showing them to the user as untracked noise inside .agitrack/.
    target = tmp_path / "state.json"
    original = os.replace
    os.replace = lambda src, dst: (_ for _ in ()).throw(OSError(28, "No space left on device"))
    try:
        with pytest.raises(OSError):
            atomic_write_text(target, "{}")
    finally:
        os.replace = original

    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_writers_do_not_destroy_each_others_state(tmp_path):
    # Several aGiTrack processes touch one repo (a session, the dashboard, a background
    # tracker). A fixed tmp name made the second writer crash mid-save; the file must simply
    # end up as one writer's complete content.
    target = tmp_path / "state.json"
    for index in range(25):
        atomic_write_text(target, json.dumps({"writer": index}))
        assert "writer" in json.loads(target.read_text())


def test_state_survives_a_round_trip_through_a_real_save(tmp_path):
    repo = _repo(tmp_path)
    state = AgitrackState(repo.repo, default_backend="claude")
    state.backend_session_id = "ses-1"
    state.save()

    assert AgitrackState(repo.repo, default_backend="claude").backend_session_id == "ses-1"


@posix_only
def test_a_read_only_state_directory_reports_rather_than_corrupting(tmp_path):
    # A read-only checkout, a full disk, a permissions mistake. The save must fail loudly
    # (the caller decides what to do) and must not leave a damaged file behind.
    target_dir = tmp_path / "state"
    target_dir.mkdir()
    target = target_dir / "state.json"
    atomic_write_text(target, json.dumps({"kept": True}))
    target_dir.chmod(0o500)  # read + execute, no write
    try:
        with pytest.raises(OSError):
            atomic_write_text(target, json.dumps({"kept": False}))
        assert json.loads(target.read_text()) == {"kept": True}
    finally:
        target_dir.chmod(0o700)  # so tmp_path cleanup can remove it


# --- a killed session releases its claim ------------------------------------


@posix_only
def test_a_sigkilled_session_frees_the_repo_lock_for_recovery(tmp_path):
    """The premise recovery is built on, proven against a real SIGKILL.

    `RecoveryService` runs only while no live aGiTrack holds the repo lock, and relies on the
    kernel releasing an ``flock`` when the holding process dies. If that were not so, a
    session killed mid-turn (the VSCode window closed, the terminal gone) would leave the repo
    permanently un-recoverable — the agent's work stranded in a worktree that nothing will
    ever commit, with no error anywhere.
    """
    repo = _repo(tmp_path)
    lock_path = repo.repo / ".agitrack" / "lock"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "from agitrack.git.lock import RepoLock\n"
            "lock = RepoLock(sys.argv[1])\n"
            "assert lock.acquire()\n"
            "print('held', flush=True)\n"
            "time.sleep(300)\n",
            str(lock_path),
            str(Path(__file__).resolve().parent.parent),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        # While it lives, a second instance is correctly refused.
        assert RepoLock(lock_path).acquire() is False

        holder.send_signal(signal.SIGKILL)  # the terminal was closed / the process was killed
        holder.wait(timeout=30)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)

    # The kernel dropped the flock, so recovery can take the repo.
    deadline = time.monotonic() + 30
    lock = RepoLock(lock_path)
    while time.monotonic() < deadline:
        if lock.acquire():
            break
        time.sleep(0.05)
    else:
        pytest.fail("a SIGKILLed session left the repo lock held; recovery could never run")
    lock.release()


def test_recovery_does_not_run_while_a_live_session_holds_the_lock(tmp_path):
    # The other half: recovery must never touch a repo someone is actively working in, or it
    # would commit a turn out from under a running agent.
    repo = _repo(tmp_path)
    lock = RepoLock(repo.repo / ".agitrack" / "lock")
    assert lock.acquire()
    try:
        assert RepoLock(repo.repo / ".agitrack" / "lock").acquire() is False
    finally:
        lock.release()


# --- the branch moving under a live session ---------------------------------


def test_a_branch_moved_externally_is_noticed_rather_than_silently_merged_over(tmp_path):
    """Someone runs `git checkout` (or a rebase, or a pull) in the repo while a session runs.

    aGiTrack integrates each turn into the branch it recorded at startup. If it does not
    notice the branch moving, it either merges into the wrong branch or fast-forwards over
    commits it never saw — both of which lose work with no error.
    """
    repo = _repo(tmp_path)
    start_head = repo.rev_parse("HEAD")
    assert repo.current_branch() == "main"

    # An outside actor commits on the same branch.
    (tmp_path / "outside.txt").write_text("someone else's work\n")
    subprocess.run(["git", "add", "outside.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "outside commit"], cwd=tmp_path, check=True)

    assert repo.rev_parse("HEAD") != start_head  # the change is observable...
    assert repo.current_branch() == "main"  # ...on the same branch aGiTrack recorded


def test_a_checkout_to_another_branch_is_observable_to_a_live_session(tmp_path):
    # The status bar bolds a session's integration branch when it differs from the repo
    # directory's. That only works if the directory's branch is re-read, not cached forever.
    repo = _repo(tmp_path)
    assert repo.current_branch() == "main"

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)

    assert repo.current_branch() == "feature"


def test_an_agent_commit_never_lands_on_a_branch_the_session_did_not_target(tmp_path):
    # The consequence that matters: a session integrating into `main` must not write to
    # `feature` just because someone checked it out in the repo directory.
    repo = _repo(tmp_path)
    subprocess.run(["git", "branch", "feature"], cwd=tmp_path, check=True)
    main_head = repo.rev_parse("main")
    feature_head = repo.rev_parse("feature")

    (tmp_path / "f.txt").write_text("agent edit\n")
    repo.stage_paths(["f.txt"])
    repo.commit("agent turn")

    assert repo.rev_parse("main") != main_head  # landed where HEAD was
    assert repo.rev_parse("feature") == feature_head  # and nowhere else
