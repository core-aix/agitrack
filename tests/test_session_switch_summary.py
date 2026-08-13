"""Switching conversation with the BACKEND's own commands must not strand summary work.

Summary state — the pending worker, its result, and the rolling ``state.session_summary`` —
is keyed by aGiTrack session, on the assumption that one aGiTrack session tracks one backend
conversation for its whole life. A native switch (`/clear`, `/resume`, OpenCode's picker)
breaks that assumption: the same aGiTrack session is repointed at a different conversation
while the previous one's summary state is still sitting in those slots.

Two user-visible failures follow, both silent:

* a summary worker started for the OLD conversation's commit lands after the switch and is
  applied — and reported — against the NEW one. Its ``sha`` is no longer the head it was
  computed for, so the amend fails and the user is told summarization failed in a session
  that never asked for a summary;
* the rolling summary still describes the old conversation, so the new one's first commit is
  summarized "in the context of" unrelated work, and that wrong context compounds into every
  later summary.

Real-git, and run on both backends: a native switch is something both support.
"""

from __future__ import annotations

import subprocess

import pytest

from agitrack.backends.proxy_agents import available_backends
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.transcripts.types import SessionRef
from proxy_helpers import make_runner

# Every registered backend, so a newly added one is covered the moment it is registered
# (the pattern test_backend_parity.py already uses).
BACKENDS = available_backends()


def _init_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _runner_tracking(tmp_path, backend_name, *, current_id, refs):
    repo = _init_repo(tmp_path)
    state = AgitrackState(repo.repo, default_backend=backend_name)
    state.backend_session_id = current_id
    runner = make_runner(repo=repo, state=state)
    runner.base_repo = repo
    runner.state.data["backend"] = backend_name

    class _Backend:
        name = backend_name

        def list_sessions(self, _repo):
            return refs

    runner.backend = _Backend()
    # The switch watcher self-throttles and refuses to run mid-turn or with the palette open.
    runner._session_watch_at = 0.0
    runner.agent_in_flight = False
    runner.running = True
    # Naming/rendering are UI; the switch bookkeeping is what is under test here.
    runner._restore_or_ask_session_name = lambda *a, **k: None
    runner._initialize_session_baseline = lambda: None
    return runner


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_native_switch_discards_the_old_conversations_pending_summary(tmp_path, backend_name):
    # The stranded-worker failure: without this, the old conversation's summary result is
    # applied to (and reported against) the conversation the user just switched TO.
    runner = _runner_tracking(
        tmp_path,
        backend_name,
        current_id="old-session",
        refs=[
            SessionRef(id="old-session", updated=100.0, label="old"),
            SessionRef(id="new-session", updated=200.0, label="new"),
        ],
    )
    runner._summary_pending = {"sha": "deadbeef", "since": 0.0}
    runner._summary_result = {"sha": "deadbeef", "summary": "about the OLD conversation"}

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "new-session"
    assert runner._summary_pending is None
    assert runner._summary_result is None


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_native_switch_clears_the_rolling_session_summary(tmp_path, backend_name):
    # The wrong-context failure. The rolling summary is INPUT to the next summary, so carrying
    # it across a switch doesn't just mislabel one commit — it poisons the whole new thread.
    runner = _runner_tracking(
        tmp_path,
        backend_name,
        current_id="old-session",
        refs=[
            SessionRef(id="old-session", updated=100.0, label="old"),
            SessionRef(id="new-session", updated=200.0, label="new"),
        ],
    )
    runner.state.session_summary = "Refactored the payment module."
    runner.state.session_summary_commit = "abc1234"

    runner._service_native_session_switch()

    assert runner.state.session_summary is None
    assert runner.state.session_summary_commit is None


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_staying_on_the_same_conversation_keeps_the_summary_state(tmp_path, backend_name):
    # The guard must be narrow. Every reactor tick calls the watcher; if it cleared state
    # whenever it ran, no session would ever accumulate a rolling summary at all.
    runner = _runner_tracking(
        tmp_path,
        backend_name,
        current_id="only-session",
        refs=[SessionRef(id="only-session", updated=100.0, label="only")],
    )
    runner.state.session_summary = "Refactored the payment module."
    runner._summary_pending = {"sha": "deadbeef", "since": 0.0}

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "only-session"
    assert runner.state.session_summary == "Refactored the payment module."
    assert runner._summary_pending == {"sha": "deadbeef", "since": 0.0}


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_stale_sibling_conversation_does_not_trigger_a_switch(tmp_path, backend_name):
    # An older conversation in the same directory must never pull tracking off the live one —
    # doing so would discard the live conversation's summary state for nothing.
    runner = _runner_tracking(
        tmp_path,
        backend_name,
        current_id="live-session",
        refs=[
            SessionRef(id="live-session", updated=500.0, label="live"),
            SessionRef(id="stale-session", updated=100.0, label="stale"),
        ],
    )
    runner.state.session_summary = "still relevant"

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "live-session"
    assert runner.state.session_summary == "still relevant"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_switch_bookkeeping_is_safe_with_no_summary_in_flight(tmp_path, backend_name):
    # The ordinary case — a switch with nothing pending — must be a clean no-op, not a crash.
    runner = _runner_tracking(
        tmp_path,
        backend_name,
        current_id="old-session",
        refs=[
            SessionRef(id="old-session", updated=100.0, label="old"),
            SessionRef(id="new-session", updated=200.0, label="new"),
        ],
    )

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "new-session"
    assert runner._summary_pending is None
