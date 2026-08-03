"""Composition-level tests: the REAL ``ProxyRunner.run()`` and reactor, end to end.

Everything else in the suite tests a method. These test the *sequence* — which is where the
bugs that survive a green suite live, because each individual method is correct and only the
order or the wiring is wrong. See ``tests/harness.py`` for why this is a separate harness.

All real-git. Nothing here sleeps: the reactor is bounded by iteration count.
"""

from __future__ import annotations

import sys

import pytest

from agitrack.proxy.platform.base import ChildProcess, HostTerminal
from harness import FakeChildProcess, FakeHostTerminal, init_repo, launch

# The harness drives a POSIX reactor (select on pipe fds, os.pipe stdin). Native Windows
# bridges those through sockets in platform/nt.py and is covered by the dedicated Windows
# job; running this here would test the fakes, not the product.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX reactor (select on pipe fds) only")


def test_fakes_satisfy_the_platform_protocols():
    # The harness is only trustworthy while its fakes match the contract the runner codes
    # against. If ChildProcess/HostTerminal grows a method, this fails here rather than
    # letting every composition test silently exercise a shape production no longer has.
    child = FakeChildProcess(["claude"], "/tmp")
    try:
        assert isinstance(child, ChildProcess)
        assert isinstance(FakeHostTerminal(), HostTerminal)
    finally:
        child.teardown()


# --- startup sequence -------------------------------------------------------


def test_startup_runs_to_the_reactor_and_spawns_the_backend(tmp_path):
    # The baseline: a plain launch reaches the reactor having spawned exactly one backend
    # child, in the repo, and returns its exit code. Without this, none of the ordering
    # assertions below mean anything.
    h = launch(tmp_path, pytest.MonkeyPatch())

    assert h.exit_code == 0
    assert h.steps.count("spawn") == 1
    assert h.spawn_command[0] == "claude"
    assert h.child.cwd == str(h.runner.repo.repo)
    assert "loop" in h.steps


def test_startup_stages_the_backend_resume_before_spawning(tmp_path):
    """The recorded regression this harness was built for.

    ``run()`` reaches ``_spawn()`` directly, while ``_new_session()`` has its own resume
    path. A fix applied to only one of them leaves the other broken, and every unit test
    stays green because both methods are individually correct. Concretely: without the
    staging call at ``runner.py:1625``, ``_spawn``'s ``_should_continue_session()`` gate
    consults the backend's RECORDED directory — which is exactly what went stale — decides
    our own session belongs to a stranger, and silently starts a FRESH conversation. The
    user loses their history with no error.

    So: assert the call happens, and that it happens BEFORE the spawn.
    """
    monkeypatch = pytest.MonkeyPatch()
    repo = init_repo(tmp_path)

    staged: list[str] = []
    from agitrack.backends import proxy_agents

    real_prepare = proxy_agents.ClaudeProxyAgent.ensure_resumable
    monkeypatch.setattr(
        proxy_agents.ClaudeProxyAgent,
        "ensure_resumable",
        lambda self, r, sid: staged.append(sid) or real_prepare(self, r, sid),
    )

    # A session recorded from a previous run is what makes this a resume rather than a
    # cold start — the whole point of the flow under test.
    from agitrack.config import AgitrackState

    state = AgitrackState(repo.repo, default_backend="claude")
    state.backend_session_id = "11111111-2222-3333-4444-555555555555"
    state.save()

    h = launch(tmp_path, monkeypatch, repo=repo)

    # Staged at least once, for OUR session id, and — the whole point — before the spawn.
    # (Later startup work stages again; what must never happen is spawning first.)
    assert staged, "the startup resume was never staged"
    assert staged[0] == "11111111-2222-3333-4444-555555555555"
    assert h.ran_before("stage-resume", "spawn"), f"staging must precede the spawn; got {h.steps}"


def test_startup_skips_resume_staging_for_a_forced_new_session(tmp_path):
    # --new-session means "do not resume": staging would retarget a conversation the user
    # explicitly asked to leave behind, so the gate at runner.py:1623 must hold.
    monkeypatch = pytest.MonkeyPatch()
    repo = init_repo(tmp_path)

    from agitrack.config import AgitrackState

    state = AgitrackState(repo.repo, default_backend="claude")
    state.backend_session_id = "11111111-2222-3333-4444-555555555555"
    state.save()

    h = launch(tmp_path, monkeypatch, repo=repo, new_session=True)

    assert "stage-resume" not in h.steps
    assert "spawn" in h.steps


def test_startup_sequence_order_is_stable(tmp_path):
    """Pin the startup ORDER, so an insertion at the wrong point fails loudly.

    Each of these orderings is load-bearing, not incidental:
      * screen before spawn — the child is sized from the screen geometry;
      * spawn before the file watcher — the watcher's baseline is "the tree as the agent
        found it";
      * git worker before reconcile — reconciliation queues work onto it;
      * hooks (base guard / manual mode) before the first paint — a turn must never be
        able to land while the hook slate is half-installed;
      * first paint before the reactor — otherwise the terminal shows whatever was there
        before until some unrelated event forces a repaint.
    """
    h = launch(tmp_path, pytest.MonkeyPatch())

    for earlier, later in (
        ("init-screen", "spawn"),
        ("spawn", "file-watcher"),
        ("file-watcher", "git-worker"),
        ("git-worker", "reconcile"),
        ("reconcile", "base-guard"),
        ("base-guard", "manual-mode"),
        ("manual-mode", "loop"),
    ):
        assert h.ran_before(earlier, later), f"{earlier} must run before {later}; got {h.steps}"
    # …and the reactor is entered on a painted screen. Reconciliation repaints earlier, so
    # this is about the LAST paint before the loop, not the first one anywhere.
    assert h.steps[h.steps.index("loop") - 1] == "render", f"the reactor must be entered painted; got {h.steps}"


def test_startup_puts_the_terminal_in_raw_mode_and_restores_it(tmp_path):
    # If raw mode is entered and not restored, the user's shell is left echo-less and
    # line-buffered after aGiTrack exits — the most user-hostile way to fail. The restore
    # lives in run()'s finally block, so only a full-run test can prove it happens.
    h = launch(tmp_path, pytest.MonkeyPatch())

    assert "alt-screen" in h.host.modes
    assert "raw" in h.host.modes
    assert "restore" in h.host.modes
    assert h.host.modes.index("raw") < h.host.modes.index("restore")
    assert h.host.stopped is True  # the host's threads were torn down too


def test_startup_restores_the_terminal_even_when_the_reactor_crashes(tmp_path):
    # The finally block is the only thing standing between a reactor bug and a wrecked
    # terminal. A crash must still restore, still release the lock, and still surface a
    # crash report rather than a silent exit.
    monkeypatch = pytest.MonkeyPatch()
    repo = init_repo(tmp_path)

    def _boom():
        raise RuntimeError("reactor exploded")

    h = launch(tmp_path, monkeypatch, repo=repo, script=lambda hh: monkeypatch.setattr(hh.runner, "_loop", _boom))

    assert h.exit_code == 1
    assert "restore" in h.host.modes
    assert h.runner._crash_notice, "a crashed reactor must leave the user a crash report"
    assert h.runner.management_lock.owner_pid() is None  # lock released for the next launch


def test_startup_releases_the_lock_so_a_second_launch_succeeds(tmp_path):
    # The management lock is what makes "already running" correct. If run() leaks it, every
    # subsequent launch in the same repo is refused until the file is deleted by hand.
    h1 = launch(tmp_path, pytest.MonkeyPatch())
    assert h1.exit_code == 0

    h2 = launch(tmp_path, pytest.MonkeyPatch(), repo=h1.runner.base_repo)
    assert h2.exit_code == 0, "the first run leaked the management lock"


def test_startup_records_the_proxy_status_and_clears_it_on_exit(tmp_path):
    # `agitrack --status` reads this file; a stale one reports a session that is long gone.
    from agitrack.proxy.background import _read_proxy_status

    seen: list[object] = []
    h = launch(
        tmp_path,
        pytest.MonkeyPatch(),
        script=lambda hh: seen.append(_read_proxy_status(hh.runner.base_repo)),
    )

    assert seen and seen[0], "status must be written while the session is live"
    assert not _read_proxy_status(h.runner.base_repo), "status must be cleared on exit"


def test_no_worktree_startup_runs_the_agent_in_the_base_repo(tmp_path):
    # --no-worktree is a different composition, not a flag on the same one: no worktree is
    # created and the child must be spawned in the base checkout. Getting this wrong strands
    # the agent's edits somewhere that is never committed.
    h = launch(tmp_path, pytest.MonkeyPatch(), use_worktrees=False)

    assert h.child.cwd == str(h.runner.base_repo.repo)
    assert not (tmp_path / ".agitrack" / "worktrees").exists()


def test_worktree_startup_runs_the_agent_inside_the_worktree(tmp_path):
    # The mirror of the above, and the default model: the agent's cwd must be the worktree,
    # never the base checkout.
    h = launch(tmp_path, pytest.MonkeyPatch(), use_worktrees=True)

    assert h.runner.worktree is not None
    assert h.child.cwd == str(h.runner.worktree.path)
    assert ".agitrack" in h.child.cwd


def test_commit_guidance_reaches_the_real_spawn_command(tmp_path):
    # The agent note is assembled deep in ClaudeProxyAgent.spawn_command from four runner
    # fields. Unit tests pin the note's text; only this proves the runner actually threads
    # its own configuration into the command it launches.
    h = launch(tmp_path, pytest.MonkeyPatch(), use_worktrees=True)

    assert "--append-system-prompt" in h.spawn_command
    note = h.spawn_command[h.spawn_command.index("--append-system-prompt") + 1]
    assert "automatically creates a git commit" in note
    assert str(h.runner.worktree.path) in note, "the note must name this session's real worktree"


def test_no_commit_guidance_omits_the_note(tmp_path):
    h = launch(tmp_path, pytest.MonkeyPatch(), commit_guidance=False)
    assert "--append-system-prompt" not in h.spawn_command


# --- the reactor ------------------------------------------------------------


def test_reactor_drains_real_backend_output_through_all_five_phases(tmp_path):
    # One full loop iteration against real select/read: the backend's bytes must reach the
    # pyte screen. This is the path every rendered frame takes.
    h = launch(
        tmp_path,
        pytest.MonkeyPatch(),
        reactor_iterations=1,
        script=lambda hh: hh.child.emit(b"hello from the backend\r\n"),
    )

    assert "loop-iteration" in h.steps
    rendered = "\n".join(h.runner.screen.display)
    assert "hello from the backend" in rendered


def test_reactor_forwards_keystrokes_to_the_backend(tmp_path):
    # Typing must reach the child. The stdin phase is 180 lines of routing (menu key, paste
    # markers, escape holds); this proves ordinary bytes still make it through all of it.
    h = launch(
        tmp_path,
        pytest.MonkeyPatch(),
        reactor_iterations=2,
        script=lambda hh: hh.host.type(b"hi"),
    )

    assert b"hi" in bytes(h.child.written)


def test_reactor_relaunches_a_backend_that_exits_on_its_own(tmp_path):
    # A backend that quits (most often Claude exiting its native session picker) must not
    # take aGiTrack down with it: the documented behaviour is to relaunch and resume. Only a
    # composition test can show this, because it spans EOF detection → relaunch → respawn.
    deaths = {"n": 0}

    def _die_once(child):
        # Kill only the FIRST child, so the relaunch has a live backend to settle on and the
        # loop reaches its iteration bound instead of the crash-loop guard.
        deaths["n"] += 1
        if deaths["n"] == 1:
            child.exit(0)

    h = launch(tmp_path, pytest.MonkeyPatch(), reactor_iterations=6, on_spawn=_die_once)

    assert h.steps.count("spawn") >= 2, f"the backend was never relaunched; got {h.steps}"
    assert "restore" in h.host.modes


def test_reactor_gives_up_on_a_backend_stuck_in_a_crash_loop(tmp_path):
    # The other half: a backend that dies immediately, every time, must NOT be relaunched
    # forever. After three deaths in twelve seconds aGiTrack stops and tells the user why —
    # otherwise the user sees an endless flicker with no explanation.
    h = launch(
        tmp_path,
        pytest.MonkeyPatch(),
        reactor_iterations=40,  # ceiling; the guard should stop it well before this
        on_spawn=lambda child: child.exit(1),
    )

    assert h.runner._backend_exit_notice, "a give-up must explain itself to the user"
    assert h.steps.count("spawn") <= 5, f"the crash-loop guard did not hold; got {h.steps.count('spawn')} spawns"
    assert "restore" in h.host.modes
