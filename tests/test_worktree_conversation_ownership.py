"""Self-heal: a worktree left holding a conversation that belongs to a sibling session.

Real residue, from the reporter's repo (2026-08-04): `worktrees/beacon`'s Claude project dir
still held `218f84f4…` after that conversation was relocated to `worktrees/firefly`, and the
root record paired firefly's conversation with the beacon worktree. Written by an aGiTrack
older than the relocation fix — and it outlives the upgrade, so opening the worktree has to
repair it rather than leaving "delete your worktrees" as the only cure.
"""

from __future__ import annotations

import subprocess

import pytest

from agitrack.config import AgitrackState, GlobalConfig
from agitrack.git import GitRepo
from agitrack.git.worktree import WorktreeManager
from agitrack.transcripts.types import SessionRef
from proxy_helpers import make_runner

MOVED = "moved-conversation"
OURS = "our-conversation"


def _init_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _runner(tmp_path, *, here_records: str | None, here_holds: list[SessionRef], named: dict[str, str]):
    """A runner opened on worktree 'alpha' while sibling 'gamma' owns MOVED."""
    base = _init_repo(tmp_path)
    manager = WorktreeManager(base)
    alpha = manager.create("alpha", base="main")
    gamma = manager.create("gamma", base="main")
    AgitrackState(gamma.path, default_backend="claude").backend_session_id = MOVED

    root = AgitrackState(base.repo, default_backend="claude")
    for sid, name in named.items():
        root.name_session(sid, name)

    state = AgitrackState(alpha.path, default_backend="claude")
    state.backend_session_id = here_records

    runner = make_runner(repo=GitRepo(alpha.path), state=state)
    runner.base_repo = base
    runner.global_config = GlobalConfig(path=tmp_path / "global.json")
    runner.worktree = alpha
    runner.name = "alpha"
    runner._use_worktrees = True
    runner._debug = lambda *a, **k: None

    forgotten: list[tuple[str, str]] = []

    class _Backend:
        name = "claude"

        def list_sessions(self, _repo):
            return here_holds

        def latest_session_id(self, _repo):
            return max(here_holds, key=lambda ref: ref.updated).id if here_holds else None

        def forget_session_in(self, repo, session_id):
            forgotten.append((str(repo), session_id))
            return True

    runner.backend = _Backend()
    runner.forgotten = forgotten
    runner.alpha, runner.gamma = alpha, gamma
    return runner


def test_a_worktree_releases_a_conversation_its_sibling_owns(tmp_path):
    # The pre-fix corruption: alpha's state still names the conversation that moved to gamma,
    # so opening alpha resumed GAMMA's conversation under alpha's name.
    runner = _runner(
        tmp_path,
        here_records=MOVED,
        here_holds=[
            SessionRef(id=MOVED, updated=200.0, label="moved"),
            SessionRef(id=OURS, updated=100.0, label="ours"),
        ],
        named={MOVED: "gamma", OURS: "alpha"},
    )

    runner._release_sibling_owned_conversations()

    assert runner.state.backend_session_id == OURS  # falls back to alpha's own work
    assert runner.state.last_backend_message_id is None
    assert runner.forgotten == [(str(runner.alpha.path), MOVED)]
    assert AgitrackState(runner.alpha.path).backend_session_id == OURS  # durable


def test_a_leftover_transcript_is_dropped_even_when_the_state_is_already_right(tmp_path):
    # The residue found live: alpha's state is correct, but the moved conversation's transcript
    # is still in alpha's project dir, where `latest_session_id` keeps finding it.
    runner = _runner(
        tmp_path,
        here_records=OURS,
        here_holds=[
            SessionRef(id=MOVED, updated=200.0, label="moved"),
            SessionRef(id=OURS, updated=100.0, label="ours"),
        ],
        named={MOVED: "gamma", OURS: "alpha"},
    )

    runner._release_sibling_owned_conversations()

    assert runner.state.backend_session_id == OURS
    assert runner.forgotten == [(str(runner.alpha.path), MOVED)]


def test_a_conversation_this_session_is_resuming_is_never_released(tmp_path):
    # Resuming a past conversation into a new session, while the worktree it used to run in is
    # still on disk, is a legitimate ask — not corruption.
    runner = _runner(
        tmp_path,
        here_records=MOVED,
        here_holds=[SessionRef(id=MOVED, updated=200.0, label="moved")],
        named={MOVED: "gamma"},
    )

    runner._release_sibling_owned_conversations(keep=MOVED)

    assert runner.state.backend_session_id == MOVED
    assert runner.forgotten == []


def test_the_name_record_decides_who_owns_a_contested_conversation(tmp_path):
    # When the durable record names US, we are the owner: the sibling is the corrupt one and
    # heals itself when it opens. Releasing here too would leave the conversation homeless.
    runner = _runner(
        tmp_path,
        here_records=MOVED,
        here_holds=[SessionRef(id=MOVED, updated=200.0, label="moved")],
        named={MOVED: "alpha"},
    )

    runner._release_sibling_owned_conversations()

    assert runner.state.backend_session_id == MOVED
    assert runner.forgotten == []


def test_a_worktree_with_no_conversation_of_its_own_starts_fresh(tmp_path):
    # Nothing left to fall back to: start a new conversation rather than keep somebody else's.
    runner = _runner(
        tmp_path,
        here_records=MOVED,
        here_holds=[SessionRef(id=MOVED, updated=200.0, label="moved")],
        named={MOVED: "gamma"},
    )

    runner._release_sibling_owned_conversations()

    assert runner.state.backend_session_id is None
    assert runner.forgotten == [(str(runner.alpha.path), MOVED)]


def test_exit_does_not_adopt_a_conversation_a_sibling_owns(tmp_path):
    # `_adopt_latest_backend_session` re-files the adopted conversation's NAME under this
    # session — the rewrite that made a later start open this worktree running the other
    # session's conversation.
    runner = _runner(
        tmp_path,
        here_records=OURS,
        here_holds=[
            SessionRef(id=MOVED, updated=200.0, label="moved"),
            SessionRef(id=OURS, updated=100.0, label="ours"),
        ],
        named={MOVED: "gamma", OURS: "alpha"},
    )

    runner._adopt_latest_backend_session()

    assert runner.state.backend_session_id == OURS
    assert AgitrackState(runner.base_repo.repo).session_name_for(MOVED) == "gamma"  # not renamed


@pytest.mark.parametrize("worktrees", [False])
def test_no_worktree_mode_has_no_siblings_to_heal_against(tmp_path, worktrees):
    runner = _runner(
        tmp_path,
        here_records=MOVED,
        here_holds=[SessionRef(id=MOVED, updated=200.0, label="moved")],
        named={MOVED: "gamma"},
    )
    runner._use_worktrees = worktrees

    runner._release_sibling_owned_conversations()

    assert runner.state.backend_session_id == MOVED
    assert runner.forgotten == []


def test_a_release_prefers_the_conversation_this_run_was_asked_to_continue(tmp_path):
    # Startup: the durable record says which conversation belongs to this worktree, while the
    # worktree's own (corrupt) state file names the sibling's. The record wins.
    runner = _runner(
        tmp_path,
        here_records=MOVED,
        here_holds=[SessionRef(id=MOVED, updated=200.0, label="moved")],
        named={MOVED: "gamma", OURS: "alpha"},
    )

    runner._release_sibling_owned_conversations(keep=OURS)

    assert runner.state.backend_session_id == OURS
    assert runner.forgotten == [(str(runner.alpha.path), MOVED)]
