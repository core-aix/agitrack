"""A conversation switched with the BACKEND's own command keeps the CURRENT worktree.

`/clear`, `/resume` and OpenCode's picker change the conversation inside the already-running
backend process. aGiTrack follows the switch, but it cannot move the process — the backend's
cwd *is* this session's worktree, and relocating it would mean respawning the backend and
killing the conversation the user just started. The new conversation therefore shares this
worktree, this session's turn branch and its merge target.

Nothing on screen said so. After the switch the status bar shows the new conversation's name
and id, which looks exactly like a session aGiTrack started itself — the kind that DOES get
its own worktree. The user only discovered the difference when two conversations' changes
turned up on one branch. These tests pin the notice that closes that gap, and the guards that
keep it from firing where it would be wrong or merely noisy.

Real git worktrees, and both backends: a native switch is something both support.
"""

from __future__ import annotations

import subprocess

import pytest

from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.git.worktree import WorktreeManager
from agitrack.transcripts.types import SessionRef
from proxy_helpers import make_runner

BACKENDS = ["claude", "opencode"]

SWITCHED = [
    SessionRef(id="old-session", updated=100.0, label="old"),
    SessionRef(id="new-session", updated=200.0, label="new"),
]


def _init_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _runner(tmp_path, backend_name, *, refs, worktree: bool):
    """A runner tracking ``old-session``, either inside a real worktree or on the base tree
    (the --no-worktree/manual shape, where there is no worktree to share)."""
    base = _init_repo(tmp_path)
    if worktree:
        info = WorktreeManager(base).create("alpha", base="main")
        session_repo = GitRepo(info.path)
    else:
        info, session_repo = None, base
    state = AgitrackState(session_repo.repo, default_backend=backend_name)
    state.backend_session_id = "old-session"

    runner = make_runner(repo=session_repo, state=state)
    runner.base_repo = base
    runner.worktree = info
    runner.name = "alpha" if worktree else "main"
    runner._use_worktrees = worktree
    runner.state.data["backend"] = backend_name

    class _Backend:
        name = backend_name

        def list_sessions(self, _repo):
            return refs

    runner.backend = _Backend()
    # The watcher self-throttles and stands down mid-turn or with the palette open.
    runner._session_watch_at = 0.0
    runner.agent_in_flight = False
    runner.running = True
    # Naming and painting are UI; the notice is what is under test.
    runner._restore_or_ask_session_name = lambda *a, **k: None
    runner._initialize_session_baseline = lambda: None
    runner._render = lambda *a, **k: None

    # The notice is a BLOCKING popup the user must acknowledge, so capture the popup rather
    # than the transient message line. Each entry is (title, options, detail-as-one-string).
    popups: list[tuple[str, list, str]] = []

    def fake_popup(title, options, *, detail=None):
        popups.append((title, list(options), "\n".join(detail or [])))
        return "ok"

    runner._select_popup = fake_popup
    runner.popups = popups
    return runner


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_native_switch_says_the_new_conversation_stays_in_this_worktree(tmp_path, backend_name):
    # The whole point: the user is told the conversation they just started inside the backend
    # did NOT get a worktree of its own, and is told which worktree it landed in instead.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True)

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "new-session"
    assert len(runner.popups) == 1
    title, options, detail = runner.popups[0]
    assert options == ["ok"]  # must be acknowledged, not left to fade on a timer
    assert "alpha" in title  # names the session whose worktree is being shared
    assert ".agitrack/worktrees/alpha" in detail
    assert backend_name in detail  # names the backend the switch happened inside of


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_notice_tells_the_user_how_to_get_a_worktree_of_its_own(tmp_path, backend_name):
    # A warning with no remedy just tells the user they have a problem. The remedy has to be
    # the real gesture: aGiTrack's own new-session action, which respawns the backend in a
    # fresh worktree. (The backend's /clear and /resume cannot do this by construction.)
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True)

    runner._service_native_session_switch()

    _title, _options, detail = runner.popups[0]
    assert runner._menu_label() in detail
    assert "sessions" in detail
    assert "New session (own worktree)" in detail


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_notice_is_shown_once_per_run_not_on_every_clear(tmp_path, backend_name):
    # A user who works in short conversations hits /clear constantly. The point is learned
    # once; repeating it on every switch would train them to dismiss aGiTrack's popups.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True)
    runner._service_native_session_switch()
    assert len(runner.popups) == 1  # first switch warns

    runner._session_watch_at = 0.0
    runner.backend.list_sessions = lambda _repo: [
        SessionRef(id="new-session", updated=200.0, label="new"),
        SessionRef(id="third-session", updated=300.0, label="third"),
    ]

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "third-session"  # the switch itself still happens
    assert len(runner.popups) == 1  # but it is not re-announced


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_end_of_turn_notice_does_not_repeat_what_the_switch_already_said(tmp_path, backend_name):
    # Two paths detect a backend-side switch: this watcher (live, between turns) and
    # _note_backend_session_change (at commit time, catching switches made mid-turn while the
    # watcher stands down). They share one flag so the user hears it once, not twice.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True)
    runner._persist_session_name = lambda *a, **k: None
    runner._record_shared_alias_on_drift = lambda *a, **k: None

    runner._service_native_session_switch()
    assert len(runner.popups) == 1

    runner._note_backend_session_change("yet-another-session")

    assert runner.message is None  # the commit-time notice stays quiet
    assert len(runner.popups) == 1


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_no_worktree_mode_says_nothing_because_there_is_no_worktree_to_share(tmp_path, backend_name):
    # --no-worktree and --manual-commits run on the base tree by design, and the user was told
    # at startup that everything shares this directory. Repeating it here would be wrong
    # (there is no worktree to name) and would fire on every conversation switch.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=False)

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "new-session"  # tracking still follows the switch
    assert runner.popups == []


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_staying_on_the_same_conversation_says_nothing(tmp_path, backend_name):
    # The watcher runs on every reactor tick. If the notice were not gated on an actual switch
    # it would paint over the screen continuously.
    runner = _runner(
        tmp_path,
        backend_name,
        refs=[SessionRef(id="old-session", updated=100.0, label="old")],
        worktree=True,
    )

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "old-session"
    assert runner.popups == []
