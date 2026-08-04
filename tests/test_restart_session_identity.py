"""A restart must come back as the session the user actually quit in.

Two durable records feed the startup decision and they are written by DIFFERENT code paths:

* ``backend_session_id`` — the resume pointer, written by ``_persist_last_session_record`` only
  for the session the user was in when they quit;
* ``backend_sessions[<backend>]`` — the per-backend record (id + worktree name), written by
  ``_remember_session_for_backend`` for EVERY session as the exit finalize walks them.

Exit finalizes the quit-in session first and the rest after it, so last-writer-wins left the
per-backend record describing an OLDER session than the resume pointer. Startup then mixed the
two: it resumed the conversation from the pointer but, when that conversation had no name of its
own, fell back to naming the session after the OTHER record's worktree. The user saw the previous
session's name with the new session's conversation running inside it — confirmed in a live
two-session run, where the exit state held ``backend_session_id`` = beta alongside
``backend_sessions.claude = {id: alpha, worktree: "alpha"}``.

Fixed at both ends, and both ends are tested here: the record is written first-writer-wins during
an exit, and the name fallback is only honoured when the record is ABOUT the conversation being
resumed.
"""

from __future__ import annotations

import subprocess

import pytest

from agitrack.config import AgitrackState, GlobalConfig
from agitrack.git import GitRepo
from agitrack.git.worktree import WorktreeInfo
from proxy_helpers import make_runner

BACKENDS = ["claude", "opencode"]


def _init_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _session(tmp_path, backend_name, name, session_id):
    """A session's own state, in its own worktree directory.

    Deliberately NOT the base repo's state file: a worktree session keeps its state inside its
    worktree, and pointing both at one file makes the two AgitrackState instances clobber each
    other's writes — an artefact of the test, not of the code under test.
    """
    path = tmp_path / ".agitrack" / "worktrees" / name
    path.mkdir(parents=True, exist_ok=True)
    state = AgitrackState(path, default_backend=backend_name)
    state.data["backend"] = backend_name
    state.backend_session_id = session_id
    return state, WorktreeInfo(name=name, path=path, branch="")


def _switch_to(runner, tmp_path, backend_name, name, session_id):
    """Point the runner at another session, as the exit loop's context swap does."""
    state, info = _session(tmp_path, backend_name, name, session_id)
    runner.state = state
    runner.name = name
    runner.worktree = info


def _runner_for(tmp_path, backend_name, *, name, session_id):
    base = _init_repo(tmp_path)
    state, info = _session(tmp_path, backend_name, name, session_id)
    runner = make_runner(repo=base, state=state)
    runner.base_repo = base
    runner.global_config = GlobalConfig(path=tmp_path / "global.json")
    runner.name = name
    runner.worktree = info
    return runner, base


# ---------------------------------------------------------------- the per-backend record


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_session_quit_in_owns_the_backend_record_not_the_one_finalized_last(tmp_path, backend_name):
    # Exit walks the quit-in session first, then the others. A later (older) session must not
    # overwrite the record — that is what made it disagree with the resume pointer.
    runner, base = _runner_for(tmp_path, backend_name, name="beta", session_id="beta-conversation")
    runner._exiting = True
    runner._remember_session_for_backend()  # the session the user quit in

    # Now the OLDER session is finalized, under the context swap the exit loop performs.
    _switch_to(runner, tmp_path, backend_name, "alpha", "alpha-conversation")
    runner._remember_session_for_backend()

    record = AgitrackState(base.repo, default_backend=backend_name).recall_session(backend_name)
    assert record["id"] == "beta-conversation"
    assert record["worktree"] == "beta"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_every_session_still_gets_its_own_name_linked(tmp_path, backend_name):
    # Only the per-backend record is first-writer-wins. Each session's name→conversation link is
    # keyed by its own id and must still be written for ALL of them, or a session resumed later
    # comes back unnamed.
    runner, base = _runner_for(tmp_path, backend_name, name="beta", session_id="beta-conversation")
    runner._exiting = True
    runner._remember_session_for_backend()
    _switch_to(runner, tmp_path, backend_name, "alpha", "alpha-conversation")
    runner._remember_session_for_backend()

    root = AgitrackState(base.repo, default_backend=backend_name)
    assert root.session_name_for("beta-conversation") == "beta"
    assert root.session_name_for("alpha-conversation") == "alpha"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_outside_an_exit_the_record_still_follows_the_current_session(tmp_path, backend_name):
    # The guard is scoped to the exit walk. During a normal run the record must keep tracking
    # whichever session is live, or switching sessions would stop updating it.
    runner, base = _runner_for(tmp_path, backend_name, name="alpha", session_id="alpha-conversation")
    runner._remember_session_for_backend()
    _switch_to(runner, tmp_path, backend_name, "beta", "beta-conversation")
    runner._remember_session_for_backend()

    record = AgitrackState(base.repo, default_backend=backend_name).recall_session(backend_name)
    assert record["id"] == "beta-conversation"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_pre_exit_write_does_not_suppress_the_first_writer_of_the_exit_walk(tmp_path, backend_name):
    # The "already recorded" set is only filled while exiting. If an ordinary in-run write also
    # filled it, the exit walk's FIRST session — the one whose record we want — would be skipped
    # and an older session would win after all, reinstating the bug.
    runner, base = _runner_for(tmp_path, backend_name, name="alpha", session_id="alpha-conversation")
    runner._remember_session_for_backend()  # a normal in-run write, not part of an exit

    runner._exiting = True
    _switch_to(runner, tmp_path, backend_name, "beta", "beta-conversation")
    runner._remember_session_for_backend()  # the session the user quit in

    record = AgitrackState(base.repo, default_backend=backend_name).recall_session(backend_name)
    assert record["id"] == "beta-conversation"
    assert record["worktree"] == "beta"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_two_backends_each_keep_their_own_record(tmp_path, backend_name):
    # First-writer-wins is PER BACKEND. A claude session and an opencode session are separate
    # answers to "what would this backend resume", and neither may suppress the other.
    other = "opencode" if backend_name == "claude" else "claude"
    runner, base = _runner_for(tmp_path, backend_name, name="beta", session_id="beta-conversation")
    runner._exiting = True
    runner._remember_session_for_backend()

    _switch_to(runner, tmp_path, other, "gamma", "gamma-conversation")
    runner._remember_session_for_backend()

    root = AgitrackState(base.repo, default_backend=backend_name)
    assert root.recall_session(backend_name)["id"] == "beta-conversation"
    assert root.recall_session(other)["id"] == "gamma-conversation"


# ---------------------------------------------------------------- the startup name fallback


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_worktree_name_from_another_conversation_is_not_borrowed(tmp_path, backend_name):
    # The reported symptom, at the decision that produced it: resuming conversation B while the
    # per-backend record still describes conversation A must NOT name the session after A's
    # worktree. Passing None for prior_worktree is what the caller now does when the ids differ.
    runner, _base = _runner_for(tmp_path, backend_name, name="alpha", session_id="alpha-conversation")
    root = AgitrackState(runner.base_repo.repo, default_backend=backend_name)
    asked: list[str] = []
    runner._prompt_startup_name = lambda continuing: asked.append("asked") or "fresh-name"

    name = runner._resolve_startup_session_name(root, "beta-conversation", None)

    assert name == "fresh-name"  # asked, rather than silently inheriting "alpha"
    assert asked == ["asked"]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_worktree_name_is_still_used_when_it_belongs_to_this_conversation(tmp_path, backend_name):
    # The fallback exists for a real case: a session whose name lived only in the last-session
    # record. When the record IS about this conversation it must still be honoured, and linked.
    runner, _base = _runner_for(tmp_path, backend_name, name="alpha", session_id="alpha-conversation")
    root = AgitrackState(runner.base_repo.repo, default_backend=backend_name)
    runner._prompt_startup_name = lambda continuing: "should-not-be-asked"

    name = runner._resolve_startup_session_name(root, "alpha-conversation", "alpha")

    assert name == "alpha"
    assert root.session_name_for("alpha-conversation") == "alpha"  # keyed by id from now on
